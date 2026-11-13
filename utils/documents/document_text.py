"""解析器共用的确定性清洗与表格行表示；不推测缺失数据。"""

import re
import unicodedata

from langchain_core.documents import Document


def clean_text(value: str) -> str:
    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x0b", "\n").replace("\x0c", "\n\n")
    text = text.replace("\u00a0", " ").replace("\ufeff", "").replace("\u200b", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # 保留缩进、制表符和行内空格，避免破坏代码、公式及纯文本表格。
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "\n".join(line.rstrip() for line in text.split("\n"))).strip("\n")


def document(text: str, source: str, locator: str = "", *, page: int = 0, extraction: str = "text", **metadata) -> list[Document]:
    """统一生成当前元数据：page 为从 1 开始的 PDF 页序，非 PDF 为 0。"""
    text = clean_text(text)
    if not text.strip():
        return []
    return [Document(page_content=text, metadata={
        "source": str(source), "locator": locator, "page": page, "extraction": extraction, **metadata,
    })]


def table_documents(rows, source: str, locator: str, *, context="", **metadata) -> list[Document]:
    """逐行保留列号及首行上下文；空值不前向填充，首行不冒充已确认的表头。"""
    rows = [[clean_text(str(cell)) if cell is not None else "" for cell in row] for row in rows]
    if not rows or not any(any(row) for row in rows):
        return []

    def render(row):
        return "；".join(f"第 {i} 列: {cell or '（空）'}" for i, cell in enumerate(row, 1))

    first = next(i for i, row in enumerate(rows) if any(row))
    result = []
    for index, row in enumerate(rows):
        if not any(row):
            continue
        prefix = f"表格前文: {clean_text(context)}\n\n" if context else ""
        if index > first:
            prefix += f"表格首个非空行（第 {first + 1} 行）: {render(rows[first])}\n\n"
        result.extend(document(f"{prefix}第 {index + 1} 行: {render(row)}", source,
                               f"{locator} · 第 {index + 1} 行", **metadata))
    return result
