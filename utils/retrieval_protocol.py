"""知识库检索的结构化协议。

为什么不再用文本短句判定：
- 文本无法区分“检索服务故障 / 知识库无资料 / 正常资料里恰好引用了这些字眼”；
- 子代理最终回答由模型生成，本身不能作为“检索是否成功”的证据；
- 因此检索工具、子代理报告、直答声明都使用带 protocol/version 的结构化数据：
  检索证据由工具写入 artifact，子代理报告由代码在其出口根据 artifact 汇总，
  主 Agent 侧只做结构校验，不再做任何失败短句子串匹配。

边界：结构校验只能保证“本轮存在证据、引用属于证据”，不能保证模型每一句结论都被
证据支持；结论级忠实度仍依赖提示词约束与人工/抽样评估。
"""

import json
import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

PROTOCOL_VERSION = 1

# 协议名由工具、提示词和输出校验共同使用，修改时需同步三者。
RETRIEVAL_PROTOCOL = "kb_retrieval"
REPORT_PROTOCOL = "kb_retrieval_report"
DIRECT_ANSWER_PROTOCOL = "kb_direct_answer"

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"
STATUS_NO_RETRIEVAL = "no_retrieval"
STATUS_INVALID = "invalid"

_REPORT_STATUSES = {
    STATUS_OK,
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_NO_RETRIEVAL,
    STATUS_INVALID,
}
_CALL_STATUSES = {STATUS_OK, STATUS_EMPTY, STATUS_ERROR, STATUS_INVALID}

RETRIEVAL_ERROR_MARKER = "__RETRIEVAL_UNAVAILABLE__"
RETRIEVAL_EMPTY_MARKER = "__RETRIEVAL_EMPTY__"

SENTINEL_KB_DOWN = "检索服务暂时不可用"
SENTINEL_KB_MISS = "未找到足够资料"
_SENTINELS = (SENTINEL_KB_DOWN, SENTINEL_KB_MISS)

DIRECT_CATEGORIES = ("greeting", "identity", "rewrite", "provided_text")

CITATION_SOURCE_LIMIT = 5
QUERY_LOG_LIMIT = 500
EVIDENCE_EXCERPT_CHARS = 120

# 行首允许 Markdown 修饰（加粗、标题、列表、引用块），否则模型把来源行写成
# "**参考来源（内部知识库）**：x.pdf" 时会绕过来源校验。
_CITATION_LINE_RE = re.compile(
    r"^[ \t\u3000>#\-*+_]*"
    r"(?:参考来源（内部知识库）|参考来源|来源)"
    r"[ \t\u3000*_]*[：:][ \t\u3000]*(.+?)[ \t\u3000]*$"
)
_SOURCE_SEP_RE = re.compile(r"[、,，;；]|\s{2,}")
_SOURCE_WRAPPERS = "`*_\"'“”‘’「」『』《》"
_SOURCE_TRAILING_PUNCT = "。.,，;；"
_SOURCE_LEADING_JUNK_RE = re.compile(r"^[-–—·•\s]+")



class RetrievalReport(BaseModel):
    """子代理报告的序列化模型。

    deepagents 的 task 工具优先调用 model_dump_json()，Pydantic 默认不转义中文，
    避免主 Agent 在上下文里读到大量 \\uXXXX。
    """

    protocol: str = REPORT_PROTOCOL
    version: int = PROTOCOL_VERSION
    status: str
    reason: str
    answer: str
    sources: list[str]
    cited_sources: list[str]
    evidence: list[dict]
    retrieval_calls: list[dict]


def build_retrieval_artifact(
    status: str,
    query: str,
    chunks: Sequence[tuple[str, str]],
) -> dict:
    """构造 do_retrieval 的结构化 artifact（不进入模型上下文，只随 ToolMessage 流转）。"""
    if status not in (STATUS_OK, STATUS_EMPTY, STATUS_ERROR):
        raise ValueError(f"非法检索状态: {status}")
    normalized = []
    if status == STATUS_OK:
        for source, content in chunks:
            if isinstance(source, str) and source.strip() and isinstance(content, str) and content.strip():
                normalized.append({"source": source.strip(), "excerpt": _excerpt(content)})
    return {
        "protocol": RETRIEVAL_PROTOCOL,
        "version": PROTOCOL_VERSION,
        "status": status,
        "query": query if isinstance(query, str) else str(query or ""),
        "chunks": normalized,
    }


def build_direct_answer_content(category: str) -> str:
    """构造直答声明的工具结果文本（模型可见，也供主 Agent 侧结构校验）。"""
    if category not in DIRECT_CATEGORIES:
        raise ValueError(f"非法直答类别: {category}")
    return json.dumps(
        {
            "protocol": DIRECT_ANSWER_PROTOCOL,
            "version": PROTOCOL_VERSION,
            "category": category,
        },
        ensure_ascii=False,
    )


def build_report_from_messages(messages: Sequence[Any]) -> dict:
    """在子代理出口汇总结构化报告：status 由检索 artifact 决定，不由模型文本决定。"""
    records = _collect_retrieval_records(messages)
    answer_body, cited_sources = split_answer_and_citations(_final_ai_text(messages))

    calls = []
    ok_records = []
    error_seen = False
    empty_seen = False
    for call_id, artifact in records:
        status = artifact.get("status")
        if status not in (STATUS_OK, STATUS_EMPTY, STATUS_ERROR):
            status = STATUS_INVALID
        query = artifact.get("query")
        query = query if isinstance(query, str) else ""
        chunks = _artifact_chunks(artifact)
        calls.append(
            {
                "tool_call_id": call_id,
                "query": query[:QUERY_LOG_LIMIT],
                "status": status,
                "chunk_count": len(chunks),
            }
        )
        if status == STATUS_OK:
            if chunks:
                ok_records.append((call_id, chunks))
            else:
                empty_seen = True
        elif status == STATUS_EMPTY:
            empty_seen = True
        elif status == STATUS_ERROR:
            error_seen = True

    sources = []
    evidence = []
    for call_id, chunks in ok_records:
        for index, chunk in enumerate(chunks):
            source = chunk["source"]
            if source not in sources:
                sources.append(source)
            evidence.append(
                {
                    "chunk_id": f"{call_id}:{index}",
                    "source": source,
                    "excerpt": chunk["excerpt"],
                }
            )

    if not records:
        status, reason = STATUS_NO_RETRIEVAL, "no_retrieval"
    elif ok_records:
        if any(not call_id for call_id, _ in ok_records):
            status, reason = STATUS_INVALID, "missing_tool_call_id"
        elif not answer_body.strip():
            status, reason = STATUS_INVALID, "missing_answer"
        elif answer_body.strip() in _SENTINELS:
            status, reason = STATUS_INVALID, "sentinel_with_evidence"
        elif any(name not in sources for name in cited_sources):
            status, reason = STATUS_INVALID, "citation_outside_evidence"
        else:
            status, reason = STATUS_OK, ""
    elif error_seen:
        status, reason = STATUS_ERROR, "retrieval_error"
    elif empty_seen:
        status, reason = STATUS_EMPTY, "no_documents"
    else:
        status, reason = STATUS_INVALID, "invalid_artifact"

    if status != STATUS_OK:
        answer_body = ""
        sources = []
        cited_sources = []
        evidence = []

    return {
        "protocol": REPORT_PROTOCOL,
        "version": PROTOCOL_VERSION,
        "status": status,
        "reason": reason,
        "answer": answer_body,
        "sources": sources,
        "cited_sources": cited_sources,
        "evidence": evidence,
        "retrieval_calls": calls,
    }



def parse_retrieval_report(text: Any) -> dict | None:
    """严格解析子代理报告；任何字段缺失、越界或自相矛盾都返回 None。"""
    data = _json_object(text)
    if data is None:
        return None
    if data.get("protocol") != REPORT_PROTOCOL or data.get("version") != PROTOCOL_VERSION:
        return None

    status = data.get("status")
    if status not in _REPORT_STATUSES:
        return None
    if not isinstance(data.get("reason"), str):
        return None

    answer = data.get("answer")
    sources = data.get("sources")
    citations = data.get("cited_sources")
    calls = data.get("retrieval_calls")
    evidence = data.get("evidence")
    if not isinstance(answer, str):
        return None
    if not _is_str_list(sources) or not _is_str_list(citations):
        return None
    if len(set(sources)) != len(sources):
        return None
    if not isinstance(calls, list) or not isinstance(evidence, list):
        return None

    call_ids = set()
    ok_call_ids = set()
    for call in calls:
        if not isinstance(call, dict):
            return None
        call_id = call.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            return None
        if call_id in call_ids:
            return None
        call_ids.add(call_id)
        call_status = call.get("status")
        if call_status not in _CALL_STATUSES:
            return None
        if call_status == STATUS_OK:
            ok_call_ids.add(call_id)
        if not isinstance(call.get("query"), str):
            return None
        if not isinstance(call.get("chunk_count"), int) or call["chunk_count"] < 0:
            return None

    evidence_sources = []
    chunk_ids = set()
    for item in evidence:
        if not isinstance(item, dict):
            return None
        source = item.get("source")
        chunk_id = item.get("chunk_id")
        if not isinstance(source, str) or not source:
            return None
        if not isinstance(chunk_id, str) or chunk_id in chunk_ids:
            return None
        if not isinstance(item.get("excerpt"), str):
            return None
        chunk_ids.add(chunk_id)
        evidence_sources.append(source)

    if status == STATUS_OK:
        if not sources or not evidence or not calls or not answer.strip():
            return None
        if not ok_call_ids:
            return None
        if any(name not in sources for name in evidence_sources):
            return None
        if set(evidence_sources) != set(sources):
            return None
        if any(name not in sources for name in citations):
            return None
        for item in evidence:
            prefix, _, index = item["chunk_id"].rpartition(":")
            if not index.isdigit() or prefix not in ok_call_ids:
                return None
    elif answer.strip() or sources or citations or evidence:
        return None

    return data


def parse_direct_answer_content(text: Any) -> str | None:
    """解析直答声明工具结果；返回类别，非法时返回 None。"""
    data = _json_object(text)
    if data is None:
        return None
    if data.get("protocol") != DIRECT_ANSWER_PROTOCOL or data.get("version") != PROTOCOL_VERSION:
        return None
    category = data.get("category")
    return category if category in DIRECT_CATEGORIES else None


def looks_like_report(text: Any) -> bool:
    """仅用于日志：判断一段文本是否声称是检索报告但解析失败。"""
    return isinstance(text, str) and REPORT_PROTOCOL in text


def split_answer_and_citations(text: str) -> tuple[str, list[str]]:
    """把回答正文与末尾的“来源：…”引用块分开，并按首次出现顺序去重。

    只把回答末尾连续的非空来源行当作引用块：用户在正文里提供的、或检索片段中
    逐字引用的“来源：”行不是引用标注，不能据此判定越界；同时 Markdown 修饰过的
    末尾来源行也必须被识别，否则会绕过来源校验。
    """
    if not isinstance(text, str) or not text:
        return "", []
    lines = text.replace("\r\n", "\n").split("\n")
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    blocks = []
    while start:
        match = _CITATION_LINE_RE.match(lines[start - 1])
        if not match:
            break
        blocks.append(_split_source_names(match.group(1)))
        start -= 1
    cited = []
    for names in reversed(blocks):
        for name in names:
            if name and name not in cited:
                cited.append(name)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines[:start])).strip()
    return body, cited



def _collect_retrieval_records(messages: Sequence[Any]) -> list[tuple[str, dict]]:
    records = []
    for message in messages or []:
        if getattr(message, "type", "") != "tool":
            continue
        artifact = getattr(message, "artifact", None)
        if not isinstance(artifact, dict):
            continue
        if artifact.get("protocol") != RETRIEVAL_PROTOCOL:
            continue
        if artifact.get("version") != PROTOCOL_VERSION:
            continue
        call_id = getattr(message, "tool_call_id", "") or ""
        records.append((str(call_id), artifact))
    return records


def _artifact_chunks(artifact: dict) -> list[dict]:
    raw = artifact.get("chunks")
    if not isinstance(raw, list):
        return []
    chunks = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        excerpt = item.get("excerpt")
        if isinstance(source, str) and source.strip() and isinstance(excerpt, str) and excerpt.strip():
            chunks.append({"source": source.strip(), "excerpt": excerpt})
    return chunks


def _final_ai_text(messages: Sequence[Any]) -> str:
    """只取最后一条 AI 消息的文本。

    若最后一条是工具消息或没有正文的 AI 消息，说明子代理没有给出最终回答；
    此时不能回退到检索前的中间叙述（那可能只是"我来查一下"之类的过程话术）。
    """
    for message in reversed(messages or []):
        message_type = getattr(message, "type", "")
        if message_type == "ai":
            return message_text(message).strip()
        if message_type == "tool":
            return ""
    return ""


def message_text(message: Any) -> str:
    """提取消息文本，兼容 str 与内容块列表两种 content 形态。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
        return "".join(parts)
    return "" if content is None else str(content)


def _json_object(text: Any) -> dict | None:
    if not isinstance(text, str):
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _split_source_names(value: str) -> list[str]:
    names = []
    for raw in _SOURCE_SEP_RE.split(value):
        name = _normalize_source_name(raw)
        if name:
            names.append(name)
    return names


def _normalize_source_name(raw: str) -> str:
    name = raw.strip()
    # 包裹符与尾随标点会互相遮挡（如“《b.pdf》。”右端先遇到句号），
    # 必须交替剥离到稳定，否则会得到"b.pdf》"这类不等于证据文件名的结果。
    for _ in range(4):
        before = name
        name = _SOURCE_LEADING_JUNK_RE.sub("", name)
        name = name.strip(_SOURCE_WRAPPERS)
        name = name.rstrip(_SOURCE_TRAILING_PUNCT).strip()
        if name == before:
            break
    return name


def _excerpt(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip()[:EVIDENCE_EXCERPT_CHARS]
