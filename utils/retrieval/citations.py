"""只把本轮检索证据的引用标记转成用户可点击的原文链接。"""

import re
from urllib.parse import parse_qs, unquote, urlsplit

from utils.retrieval.source_evidence import passage_digest, text_digest

CITATION_RE = re.compile(r"\[\[cite:([0-9a-f]{64})\]\]")
_SOURCE_PATH_RE = re.compile(r"/api/projects/[0-9a-f-]{36}/sources/[^/]+")


def valid_evidence(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    if any(not isinstance(item.get(key), str) or not item[key].strip()
           for key in ("citation_id", "source", "excerpt", "location", "source_url")):
        return False
    try:
        url = urlsplit(item["source_url"])
    except ValueError:
        return False
    if url.scheme or url.netloc or url.fragment or not _SOURCE_PATH_RE.fullmatch(url.path):
        return False
    if unquote(url.path.rsplit("/", 1)[-1]) != item["source"]:
        return False
    query = parse_qs(url.query)
    if set(query) != {"version", "unit", "start", "end", "digest"} or any(len(v) != 1 for v in query.values()):
        return False
    if any(not re.fullmatch(r"[0-9a-f]{64}", query[k][0]) for k in ("version", "digest")):
        return False
    if any(not re.fullmatch(r"\d{1,12}", query[k][0]) for k in ("unit", "start", "end")):
        return False
    return (int(query["end"][0]) - int(query["start"][0]) == len(item["excerpt"])
            and passage_digest(item["excerpt"], item["location"], int(query["unit"][0]),
                               int(query["start"][0]), int(query["end"][0])) == query["digest"][0]
            and text_digest(item["source_url"]) == item["citation_id"])


def cited_evidence_ids(text: str, evidence: list[dict]) -> list[str]:
    """缺失、残缺或越界标记拒绝输出；不猜测哪份证据支持某条结论。"""
    ids = list(dict.fromkeys(CITATION_RE.findall(text)))
    remaining = CITATION_RE.sub("", text)
    if not ids or "[[cite:" in remaining or "/api/projects/" in remaining:
        raise ValueError("回答缺少有效的证据标记或自行拼接了原文链接")
    allowed = {item["citation_id"] for item in evidence}
    if any(identifier not in allowed for identifier in ids):
        raise ValueError("回答引用了本轮证据之外的片段")
    if not remaining.strip():
        raise ValueError("回答只有引用，没有结论")
    # 代码或 HTML 中的标记属于字面内容，不能当作结论的证据绑定。
    for block in re.findall(r"```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]*`|<[^>]*>", text):
        if CITATION_RE.search(block):
            raise ValueError("引用标记不能放在代码或 HTML 中")
    return ids


def _markdown_text(text: str) -> str:
    # 文件名作为文本呈现，不执行其中的 Markdown/HTML。
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#+.!|~>-])", r"\\\1", escaped)


def render_citations(text: str, evidence: list[dict]) -> str:
    ids = cited_evidence_ids(text, evidence)
    by_id = {item["citation_id"]: item for item in evidence}
    # 标记只用于上面的证据校验；用户正文不显示角标，页段坐标保留在文末链接中。
    body = CITATION_RE.sub("", text).strip()
    blocks = []
    for identifier in ids:
        item = by_id[identifier]
        label = _markdown_text(item['source'])
        blocks.append(f"- [{label}]({item['source_url']})")
    return f"{body}\n\n来源：\n\n" + "\n".join(blocks)
