"""本地中英文 OCR 与有边框扫描表格提取。模型调用串行化，避免共享状态竞争。"""

import threading
import re
from bisect import bisect_right

import cv2
import numpy as np
from PIL import Image, ImageOps

from utils.document_text import document, table_documents

_engine = None
_lock = threading.Lock()
MAX_IMAGE_PIXELS = 40_000_000


def _settings():
    from utils.config_loader import vector_db_config
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
    global _engine
    from rapidocr import RapidOCR

    image = prepare_image(image)
    bgr = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
    settings = _settings()
    with _lock:
        if _engine is None:
            _engine = RapidOCR(params={
                "Global.text_score": settings["ocr_min_confidence"],
                "Global.max_side_len": settings["ocr_max_side"],
                "Global.log_level": "warning",
                "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            })
        output = _engine(bgr)
    if output.txts is None:
        return []
    blocks = []
    for box, text, score in zip(output.boxes, output.txts, output.scores):
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
