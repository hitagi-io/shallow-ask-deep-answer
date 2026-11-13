"""本地中英文 OCR 与有边框扫描表格提取。模型调用串行化，避免共享状态竞争。"""

import threading
import re
import logging
import time
import hashlib
from bisect import bisect_right
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from utils.documents.document_text import document, table_documents
from utils.core.logging_config import get_logger
from utils.runtime.shutdown import check_shutdown, interruptible_lock

logger = get_logger(__name__)
_engines = {}
_lock = threading.Lock()
MAX_IMAGE_PIXELS = 40_000_000


class _EmptyDetectionFilter(logging.Filter):
    def filter(self, record):
        # 插图没有文字是正常结果；只合并这一条，模型和运行错误仍照常输出。
        return not (record.levelno == logging.WARNING
                    and record.getMessage() == "The text detection result is empty")


logging.getLogger("RapidOCR").addFilter(_EmptyDetectionFilter())


@dataclass
class _OCRProgress:
    source: str
    count: int = 0
    empty: int = 0
    skipped: int = 0
    reused: int = 0
    results: OrderedDict = field(default_factory=OrderedDict)
    started: float = field(default_factory=time.monotonic)
    last_log: float = field(default_factory=time.monotonic)


_progress: ContextVar[_OCRProgress | None] = ContextVar("ocr_progress", default=None)


@contextmanager
def document_ocr_progress(source: str):
    progress = _OCRProgress(Path(source).name)
    token = _progress.set(progress)
    state = "未完成"
    logger.info("[文档解析] 开始 file=%s", progress.source)
    try:
        yield
        state = "完成"
    finally:
        _progress.reset(token)
        logger.info("[文档解析] %s file=%s 图片=%s OCR=%s 空结果=%s 纯色跳过=%s 复用=%s 耗时=%.1fs",
                    state, progress.source, progress.count,
                    progress.count - progress.skipped - progress.reused,
                    progress.empty, progress.skipped, progress.reused,
                    time.monotonic() - progress.started)


def _record_ocr(empty: bool, locator: str, *, skipped=False, reused=False) -> None:
    progress = _progress.get()
    if progress is None:
        return
    progress.count += 1
    progress.empty += int(empty)
    progress.skipped += int(skipped)
    progress.reused += int(reused)
    now = time.monotonic()
    if now - progress.last_log >= 30:
        logger.info("[OCR] 处理中 file=%s 已处理=%s 空结果=%s 位置=%s",
                    progress.source, progress.count, progress.empty, locator)
        progress.last_log = now


def _settings():
    from utils.core.config_loader import vector_db_config
    return vector_db_config["parsing"]


def prepare_image(image: Image.Image) -> Image.Image:
    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise ValueError("图片超过 4000 万像素，请缩小后上传")
    image = ImageOps.exif_transpose(image)
    # 透明截图按白色背景合成，避免透明文字被当作黑底。
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        image = Image.alpha_composite(background, rgba)
    image = image.convert("RGB")
    image.thumbnail((_settings()["ocr_max_side"],) * 2, Image.Resampling.LANCZOS)
    return image


def _inference_image(bgr):
    """小文字适度放大，细长图受最长边约束；补白避免检测网络将短边舍入为零。"""
    height, width = bgr.shape[:2]
    limit = _settings()["ocr_max_side"] // 32 * 32
    ratio = min(max(1.0, 736 / min(height, width)), limit / max(height, width))
    target_width, target_height = max(1, round(width * ratio)), max(1, round(height * ratio))
    resized = cv2.resize(bgr, (target_width, target_height)) if (target_width, target_height) != (width, height) else bgr
    pad_x, pad_y = max(0, 32 - target_width), max(0, 32 - target_height)
    left, top = pad_x // 2, pad_y // 2
    if pad_x or pad_y:
        resized = cv2.copyMakeBorder(resized, top, pad_y - top, left, pad_x - left,
                                    cv2.BORDER_CONSTANT, value=(255, 255, 255))
    return resized, (width / target_width, height / target_height), (left, top)


def _axis_lines(mask, axis, minimum):
    points = np.flatnonzero(np.count_nonzero(mask, axis=axis) >= minimum)
    groups = np.split(points, np.where(np.diff(points) > 2)[0] + 1)
    return [int(round(float(group.mean()))) for group in groups if len(group)]


def _grids(bgr):
    """只将存在明确横纵边框的区域识别为表格；不猜测无边框表格。"""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    h, w = gray.shape
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(25, w // 30), 1)))
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(25, h // 30))))
    contours, _ = cv2.findContours(horizontal | vertical, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    grids = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width < 100 or height < 45:
            continue
        xs = [x + value for value in _axis_lines(vertical[y:y+height, x:x+width], 0, height * .6)]
        ys = [y + value for value in _axis_lines(horizontal[y:y+height, x:x+width], 1, width * .6)]
        if len(xs) >= 3 and len(ys) >= 3:
            grids.append((xs, ys))
    return sorted(grids, key=lambda grid: (grid[1][0], grid[0][0]))


def ocr_documents(image: Image.Image, source: str, locator: str, *, existing_text="", **metadata):
    check_shutdown()
    image = prepare_image(image)
    check_shutdown()
    # 纯色图片不可能包含文字。Office 文档中大量细线和空白占位图无需运行检测模型。
    if all(low == high for low, high in image.getextrema()):
        _record_ocr(True, locator, skipped=True)
        return []
    bgr = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
    settings = _settings()
    progress = _progress.get()
    key = (bgr.shape, hashlib.sha256(memoryview(bgr)).digest()) if progress else None
    cached = progress.results.get(key) if progress else None
    if cached is None:
        from rapidocr import RapidOCR
        with interruptible_lock(_lock):
            # 细长图使用受限输入，其他图片直接识别；两类模型共用调用锁。
            limit = settings["ocr_max_side"] // 32 * 32
            bounded = max(image.size) * max(1.0, 736 / min(image.size)) > limit
            if bounded not in _engines:
                params = {
                    "Global.text_score": settings["ocr_min_confidence"],
                    "Global.max_side_len": settings["ocr_max_side"],
                    "Global.log_level": "warning",
                    "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                }
                if bounded:
                    params.update({
                        "Global.use_preprocess_img": False,
                        "Global.use_vertical_padding": False,
                        "Det.limit_type": "max",
                        "Det.limit_side_len": limit,
                    })
                _engines[bounded] = RapidOCR(params=params)
            check_shutdown()
            inference, scale, offset = _inference_image(bgr) if bounded else (bgr, (1, 1), (0, 0))
            output = _engines[bounded](inference)
            # 只缓存识别结果，避免保留原图、裁图和已有文件的定位元数据。
            texts = tuple(output.txts) if output.txts is not None else ()
            boxes = np.array(output.boxes, dtype=float, copy=True) if texts else ()
            if texts:
                boxes[..., 0] = np.clip((boxes[..., 0] - offset[0]) * scale[0], 0, image.width)
                boxes[..., 1] = np.clip((boxes[..., 1] - offset[1]) * scale[1], 0, image.height)
            scores = tuple(output.scores) if texts else ()
        if progress:
            progress.results[key] = (boxes, texts, scores)
            if len(progress.results) > 256:
                progress.results.popitem(last=False)
    else:
        boxes, texts, scores = cached
        progress.results.move_to_end(key)
    _record_ocr(not texts, locator, reused=cached is not None)
    check_shutdown()
    if not texts:
        return []
    blocks = []
    for box, text, score in zip(boxes, texts, scores):
        if float(score) < settings["ocr_min_confidence"] or not text.strip():
            continue
        points = np.asarray(box)
        blocks.append({"x": float(points[:, 0].mean()), "y": float(points[:, 1].mean()),
                       "height": float(np.ptp(points[:, 1])), "text": text})
    blocks.sort(key=lambda item: (item["y"], item["x"]))
    if existing_text:
        # 忽略行内空格差异，但必须匹配完整文字行，不能将短数字当作另一数字的子串。
        native_lines = {re.sub(r"\s+", "", line) for line in existing_text.splitlines()}
        if all(re.sub(r"\s+", "", block["text"]) in native_lines for block in blocks):
            return []
    used = set()
    ordered = []
    grids = _grids(bgr)
    for number, (xs, ys) in enumerate(grids, 1):
        rows = [[[] for _ in range(len(xs) - 1)] for _ in range(len(ys) - 1)]
        for index, block in enumerate(blocks):
            if xs[0] <= block["x"] <= xs[-1] and ys[0] <= block["y"] <= ys[-1]:
                row = min(bisect_right(ys, block["y"]) - 1, len(rows) - 1)
                col = min(bisect_right(xs, block["x"]) - 1, len(xs) - 2)
                rows[row][col].append(block["text"])
                used.add(index)
        values = [[" ".join(cell) for cell in row] for row in rows]
        preceding = [block["text"] for index, block in enumerate(blocks)
                     if index not in used and 0 < ys[0] - block["y"] < image.height * .3]
        context = "\n".join(preceding[-3:])[-300:]
        ordered.append((ys[0], table_documents(values, source, f"{locator} · OCR 表格 {number}",
                                             context=context, extraction="ocr", **metadata)))
    # 同一视觉行内按横坐标排序，保留空隙形成的列分隔。
    lines = []
    for index, block in enumerate(blocks):
        if index in used:
            continue
        if lines and abs(block["y"] - lines[-1][0]["y"]) <= max(4, block["height"] * .5):
            lines[-1].append(block)
        else:
            lines.append([block])
    # 连续正文合为单元，避免一段话的条件和结论因换行而失去检索上下文。
    groups = []
    for number, line in enumerate(lines, 1):
        y = line[0]["y"]
        if not groups or len(groups[-1]) >= 12 or any(groups[-1][-1][1] < ys[0] < y for _, ys in grids):
            groups.append([])
        groups[-1].append((number, y, "  ".join(block["text"] for block in sorted(line, key=lambda item: item["x"]))))
    for group in groups:
        text = "\n".join(item[2] for item in group)
        ordered.append((group[0][1], document(text, source,
                        f"{locator} · OCR 文本行 {group[0][0]}–{group[-1][0]}", extraction="ocr", **metadata)))
    return [doc for _, docs in sorted(ordered, key=lambda item: item[0]) for doc in docs]
