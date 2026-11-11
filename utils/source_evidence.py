"""原文坐标与可核验引用。页码从 1 开始，段落按提取文本的空行划分。"""

import hashlib
import os
import re
from bisect import bisect_left, bisect_right
from urllib.parse import quote, urlencode


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def source_location(text: str, start: int, end: int, page: int, *, spans=None) -> str:
    spans = paragraph_spans(text) if spans is None else spans
    first = bisect_right(spans, start, key=lambda span: span[1]) + 1
    last = bisect_left(spans, end, key=lambda span: span[0])
    if first > last:
        raise ValueError("引用范围没有正文")
    numbers = str(first) if first == last else f"{first}–{last}"
    prefix = f"第 {page} 页 · " if page else ""
    return f"{prefix}第 {numbers} 段（提取文本）"


def split_located_documents(documents, splitter):
    """使用 splitter 的真实字符偏移，保留页面/文档单元，拒绝猜测位置。"""
    chunks = []
    for unit, document in enumerate(documents):
        text = document.page_content
        spans = paragraph_spans(text)
        page = document.metadata.get("page")
        page_number = page + 1 if type(page) is int and page >= 0 else 0
        for chunk in splitter.split_documents([document]):
            start = chunk.metadata.get("start_index")
            if type(start) is not int or start < 0:
                raise ValueError("分块缺少原文字符偏移")
            end = start + len(chunk.page_content)
            if text[start:end] != chunk.page_content:
                raise ValueError("分块与原文字符偏移不一致")
            chunk.metadata.update({
                "source_unit": unit, "source_start": start, "source_end": end,
                "source_page": page_number,
                "source_location": source_location(text, start, end, page_number, spans=spans),
            })
            chunks.append(chunk)
    return chunks


def make_source_evidence(document, project_id: str) -> dict:
    metadata = document.metadata
    source = os.path.basename(metadata["source"])
    if not source.strip() or not document.page_content.strip():
        raise ValueError("检索分块缺少有效来源或正文")
    query = {
        "version": metadata["content_sha256"], "unit": metadata["source_unit"],
        "start": metadata["source_start"], "end": metadata["source_end"],
        "digest": metadata["chunk_sha256"],
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
    if not 0 <= start < end <= len(text) or text_digest(text[start:end]) != digest:
        raise SourceVersionChanged("原文解析结果已变化，无法定位该证据。请重新提问。")
    if compute_file_sha256(file_path) != version:
        raise SourceVersionChanged("读取期间原文件发生变化，请重试。")
    page = document.metadata.get("page")
    page_number = page + 1 if type(page) is int and page >= 0 else 0
    return {
        "source": os.path.basename(file_path), "version": version,
        "location": source_location(text, start, end, page_number),
        "page": page_number or None, "excerpt": text[start:end],
        "before": text[max(0, start - 500):start], "after": text[end:end + 500],
        "clipped_before": start > 500, "clipped_after": end + 500 < len(text),
    }
