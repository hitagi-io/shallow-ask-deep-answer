"""可核验的提取文本坐标，保留页序、表格单元格、幻灯片和 OCR 定位。"""

import hashlib
import json
import os
import re
from bisect import bisect_left, bisect_right
from urllib.parse import quote, urlencode


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SOURCE_FIELDS = ("source_unit", "source_start", "source_end", "page", "locator", "extraction", "source_location")


def passage_digest(text: str, location: str, unit: int, start: int, end: int) -> str:
    """证据协议及原文回读共同校验正文、可见位置和字符范围。"""
    return text_digest(json.dumps([text, location, unit, start, end], ensure_ascii=False))


def chunk_signature(text: str, metadata: dict) -> str:
    """清单绑定正文与引用坐标，拒绝缺字段的旧块及已损坏的当前块。"""
    values = {field: metadata[field] for field in SOURCE_FIELDS}
    if (not isinstance(text, str) or not text.strip()
            or any(type(values[field]) is not int or values[field] < 0
                   for field in ("source_unit", "source_start", "source_end", "page"))
            or values["source_end"] - values["source_start"] != len(text)
            or not isinstance(values["locator"], str)
            or values["extraction"] not in ("text", "ocr")
            or not isinstance(values["source_location"], str) or not values["source_location"].strip()):
        raise ValueError("分块原文坐标无效")
    return text_digest(json.dumps([text, values], sort_keys=True, ensure_ascii=False))


def paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    start = 0
    for boundary in re.finditer(r"\r?\n[ \t\r]*\n(?:[ \t\r]*\n)*", text):
        if text[start:boundary.start()].strip():
            spans.append((start, boundary.start()))
        start = boundary.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def source_location(text: str, start: int, end: int, page: int, *, locator: str, extraction: str, spans=None) -> str:
    spans = paragraph_spans(text) if spans is None else spans
    first = bisect_right(spans, start, key=lambda span: span[1]) + 1
    last = bisect_left(spans, end, key=lambda span: span[0])
    if first > last:
        raise ValueError("引用范围没有正文")
    numbers = str(first) if first == last else f"{first}–{last}"
    prefix = f"第 {page} 页 · " if page else ""
    if locator:
        prefix += locator + " · "
    kind = "OCR 识别文本" if extraction == "ocr" else "提取文本"
    return f"{prefix}第 {numbers} 段（{kind}）"


def split_located_documents(documents, splitter):
    """使用 splitter 的真实字符偏移，保留页面/文档单元，拒绝猜测位置。"""
    chunks = []
    for unit, document in enumerate(documents):
        text = document.page_content
        spans = paragraph_spans(text)
        page_number = document.metadata["page"]
        for chunk in splitter.split_documents([document]):
            start = chunk.metadata.pop("start_index", None)
            if type(start) is not int or start < 0:
                raise ValueError("分块缺少原文字符偏移")
            end = start + len(chunk.page_content)
            if text[start:end] != chunk.page_content:
                raise ValueError("分块与原文字符偏移不一致")
            chunk.metadata.update({
                "source_unit": unit, "source_start": start, "source_end": end,
                "source_location": source_location(text, start, end, page_number, spans=spans,
                    locator=document.metadata["locator"], extraction=document.metadata["extraction"]),
            })
            chunks.append(chunk)
    return chunks


def make_source_evidence(document, project_id: str) -> dict:
    metadata = document.metadata
    chunk_signature(document.page_content, metadata)
    if metadata["chunk_sha256"] != text_digest(document.page_content):
        raise ValueError("检索分块正文摘要不一致")
    source = os.path.basename(metadata["source"])
    if not source.strip():
        raise ValueError("检索分块缺少有效来源")
    query = {
        "version": metadata["content_sha256"], "unit": metadata["source_unit"],
        "start": metadata["source_start"], "end": metadata["source_end"],
        "digest": passage_digest(document.page_content, metadata["source_location"], metadata["source_unit"],
                                 metadata["source_start"], metadata["source_end"]),
    }
    url = f"/api/projects/{project_id}/sources/{quote(source, safe='')}?{urlencode(query)}"
    return {
        "citation_id": text_digest(url), "source": source,
        "excerpt": document.page_content, "location": metadata["source_location"],
        "source_url": url,
    }


class SourceVersionChanged(ValueError):
    pass


def read_source_passage(file_path: str, version: str, unit: int, start: int, end: int, digest: str) -> dict:
    # 延迟导入，坐标和引用协议不依赖文档解析器或外部服务。
    from utils.knowledge_files import compute_file_sha256, get_document_loader

    if not os.path.isfile(file_path):
        raise FileNotFoundError("原文件不存在或已删除")
    if compute_file_sha256(file_path) != version:
        raise SourceVersionChanged("原文件已更新，无法用当前版本核验这条历史引用。请重新提问。")
    loader = get_document_loader(file_path)
    if loader is None:
        raise ValueError("不支持的原文件类型")
    documents = loader(file_path)
    if not 0 <= unit < len(documents):
        raise ValueError("引用页段不存在")
    document = documents[unit]
    text = document.page_content
    if not 0 <= start < end <= len(text):
        raise SourceVersionChanged("原文解析结果已变化，无法定位该证据。请重新提问。")
    page_number = document.metadata["page"]
    location = source_location(text, start, end, page_number, locator=document.metadata["locator"],
                               extraction=document.metadata["extraction"])
    if passage_digest(text[start:end], location, unit, start, end) != digest:
        raise SourceVersionChanged("原文解析结果已变化，无法定位该证据。请重新提问。")
    if compute_file_sha256(file_path) != version:
        raise SourceVersionChanged("读取期间原文件发生变化，请重试。")
    return {
        "source": os.path.basename(file_path), "version": version,
        "location": location,
        "extraction": document.metadata["extraction"],
        "page": page_number or None, "excerpt": text[start:end],
        "before": text[max(0, start - 500):start], "after": text[end:end + 500],
        "clipped_before": start > 500, "clipped_after": end + 500 < len(text),
    }
