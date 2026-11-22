"""通过 Any2Markdown 和本地 Xiaomi-OCR-0 识别扫描页和内嵌图片。"""

import hashlib
import re
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from utils.core.logging_config import get_logger
from utils.documents.any2markdown_backend import convert_png
from utils.runtime.shutdown import check_shutdown

logger = get_logger(__name__)
MAX_IMAGE_PIXELS = 40_000_000


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
        yield progress
        state = "完成"
    finally:
        _progress.reset(token)
        logger.info("[文档解析] %s file=%s 图片=%s OCR=%s 空结果=%s 纯色跳过=%s 复用=%s 耗时=%.1fs",
                    state, progress.source, progress.count,
                    progress.count - progress.skipped - progress.reused,
                    progress.empty, progress.skipped, progress.reused,
                    time.monotonic() - progress.started)


def include_ocr_counts(count: int, empty: int, skipped: int, reused: int) -> None:
    """将工作簿子进程的统计归入当前文档的解析日志。"""
    progress = _progress.get()
    if progress is not None:
        progress.count += count
        progress.empty += empty
        progress.skipped += skipped
        progress.reused += reused


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
    # PNG 的 EXIF 可能位于像素数据之后，解码完成后再判断是否需要转向、合成或缩放。
    image.load()
    limit = _settings()["ocr_max_side"]
    if image.mode == "RGB" and not image.info and max(image.size) <= limit and not image.getexif():
        return image
    image = ImageOps.exif_transpose(image)
    # 透明截图按白色背景合成，避免透明文字被当作黑底。
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        image = Image.alpha_composite(background, rgba)
    image = image.convert("RGB")
    image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
    return image


def ocr_documents(image: Image.Image, source: str, locator: str, *, existing_text="",
                  original_stream=None, cache_image=True, **metadata):
    from utils.documents.document_parsing import markdown_documents

    check_shutdown()
    original = image
    image = prepare_image(original)
    if all(low == high for low, high in image.getextrema()):
        _record_ocr(True, locator, skipped=True)
        return []

    progress = _progress.get()
    key = (image.size, hashlib.sha256(image.tobytes()).digest()) if progress and cache_image else None
    cached = key is not None and key in progress.results
    if cached:
        markdown = progress.results[key]
        progress.results.move_to_end(key)
    else:
        if (original_stream is not None and image is original and image.format == "PNG"
                and getattr(image, "n_frames", 1) == 1):
            # 已校验的无元数据 RGB PNG 与规范化图像逐像素相同，直接复用源字节。
            original_stream.seek(0)
            markdown = convert_png(original_stream)
        else:
            with BytesIO() as stream:
                image.save(stream, format="PNG", compress_level=1)
                stream.seek(0)
                markdown = convert_png(stream)
        if key is not None:
            progress.results[key] = markdown
            if len(progress.results) > 256:
                progress.results.popitem(last=False)

    check_shutdown()
    docs = markdown_documents(markdown, source, locator, extraction="ocr", **metadata)
    _record_ocr(not docs, locator, reused=cached)
    if existing_text:
        # 只按完整行去重，避免误删同页中的另一个价格、编号或表格字段。
        native_lines = {re.sub(r"\s+", "", line) for line in existing_text.splitlines()}
        docs = [doc for doc in docs if any(
            re.sub(r"\s+", "", line) not in native_lines
            for line in doc.page_content.splitlines() if line.strip()
        )]
    return docs
