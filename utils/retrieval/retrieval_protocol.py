"""知识库检索的结构化协议。

为什么区分工具状态与模型正文：
- 文本无法区分“检索服务故障 / 知识库无资料 / 正常资料里恰好引用了这些字眼”；
- 子代理最终回答由模型生成，本身不能作为“检索是否成功”的证据；精确的弃答
  信号只能降级为无足够依据，不能授权事实输出，服务故障仍须有工具记录；
- 因此检索工具、子代理报告、澄清请求都使用带 protocol/version 的结构化数据：
  检索证据由工具写入 artifact，子代理报告由代码在其出口根据 artifact 汇总，
  主 Agent 侧只做结构校验，不再做任何失败短句子串匹配。

边界：结构校验保证“本轮存在证据、引用绑定片段与原文坐标”，不能保证模型每一句结论都被
证据支持；结论级忠实度仍依赖提示词约束与人工/抽样评估。
"""

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import BaseModel
from utils.retrieval.citations import cited_evidence_ids, valid_evidence

PROTOCOL_VERSION = 2

# 协议名由工具、提示词和输出校验共同使用，修改时需同步三者。
RETRIEVAL_PROTOCOL = "kb_retrieval"
REPORT_PROTOCOL = "kb_retrieval_report"
CLARIFICATION_PROTOCOL = "kb_clarification"

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

SENTINEL_KB_DOWN = "检索服务暂时不可用"
SENTINEL_KB_MISS = "未找到足够资料"
_SENTINELS = (SENTINEL_KB_DOWN, SENTINEL_KB_MISS)

QUERY_LOG_LIMIT = 500



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


def build_retrieval_content(status: str, chunks: Sequence[dict]) -> str:
    """模型可见的工具结果：代码状态与不可信资料分字段，转义嵌入的结构字符。"""
    if status not in (STATUS_OK, STATUS_EMPTY, STATUS_ERROR):
        raise ValueError(f"非法检索状态: {status}")
    return json.dumps(
        {
            "status": status,
            "documents": [
                {"source": chunk["source"], "content": chunk["excerpt"],
                 "citation_id": chunk["citation_id"], "location": chunk["location"]}
                for chunk in chunks
            ] if status == STATUS_OK else [],
        },
        ensure_ascii=False,
    )


def build_retrieval_artifact(
    status: str,
    query: str,
    chunks: Sequence[dict],
) -> dict:
    """构造 do_retrieval 的结构化 artifact（不进入模型上下文，只随 ToolMessage 流转）。"""
    if status not in (STATUS_OK, STATUS_EMPTY, STATUS_ERROR):
        raise ValueError(f"非法检索状态: {status}")
    return {
        "protocol": RETRIEVAL_PROTOCOL,
        "version": PROTOCOL_VERSION,
        "status": status,
        "query": query,
        "chunks": [
            dict(chunk) for chunk in chunks
        ] if status == STATUS_OK else [],
    }


def build_clarification_content(question: str) -> str:
    """封装待用户回答的澄清问句，不授权任何无依据的正文。"""
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        raise ValueError("澄清问题须为 1～500 字")
    return json.dumps(
        {
            "protocol": CLARIFICATION_PROTOCOL,
            "version": PROTOCOL_VERSION,
            "question": question.strip(),
        },
        ensure_ascii=False,
    )


def build_report_from_messages(messages: Sequence[BaseMessage]) -> dict:
    """按工具记录与引用汇总报告；允许精确弃答，不能凭模型文本伪造检索或故障。"""
    records = _collect_retrieval_records(messages)

    calls = []
    ok_records = []
    error_seen = False
    empty_seen = False
    for call_id, artifact in records:
        status = artifact.get("status")
        if status not in (STATUS_OK, STATUS_EMPTY, STATUS_ERROR):
            status = STATUS_INVALID
        query = artifact["query"]
        chunks = artifact["chunks"]
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
                    **chunk,
                    "chunk_id": f"{call_id}:{index}",
                }
            )

    answer_body = _final_ai_text(messages)
    cited_sources = []
    citation_error = False
    if ok_records:
        try:
            if any(not valid_evidence(item) for item in evidence):
                raise ValueError("证据缺少可核验的原文位置")
            cited_ids = cited_evidence_ids(answer_body, evidence)
            by_id = {item["citation_id"]: item for item in evidence}
            cited_sources = list(dict.fromkeys(by_id[identifier]["source"] for identifier in cited_ids))
        except ValueError:
            citation_error = True
    if not records:
        status, reason = STATUS_NO_RETRIEVAL, "no_retrieval"
    elif answer_body == SENTINEL_KB_DOWN and error_seen:
        # 召回片段未必能回答问题；允许弃答，但不能凭模型文本伪造服务故障。
        status, reason = STATUS_ERROR, "retrieval_error"
    elif answer_body == SENTINEL_KB_MISS and not error_seen and (ok_records or empty_seen):
        status, reason = STATUS_EMPTY, "no_relevant_evidence" if ok_records else "no_documents"
    elif ok_records:
        if any(not call_id for call_id, _ in ok_records):
            status, reason = STATUS_INVALID, "missing_tool_call_id"
        elif not answer_body.strip():
            status, reason = STATUS_INVALID, "missing_answer"
        elif answer_body.strip() in _SENTINELS:
            status, reason = STATUS_INVALID, "sentinel_with_evidence"
        elif citation_error:
            status, reason = STATUS_INVALID, "missing_or_invalid_citation"
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
        if not valid_evidence(item):
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
        try:
            cited_ids = cited_evidence_ids(answer, evidence)
        except ValueError:
            return None
        by_id = {item["citation_id"]: item for item in evidence}
        if set(citations) != {by_id[identifier]["source"] for identifier in cited_ids}:
            return None
    elif answer.strip() or sources or citations or evidence:
        return None

    return data


def parse_clarification_content(text: Any) -> str | None:
    """解析澄清工具结果；返回问句，非法时返回 None。"""
    data = _json_object(text)
    if data is None:
        return None
    if data.get("protocol") != CLARIFICATION_PROTOCOL or data.get("version") != PROTOCOL_VERSION:
        return None
    question = data.get("question")
    return question.strip() if isinstance(question, str) and 0 < len(question.strip()) <= 500 else None


def looks_like_report(text: Any) -> bool:
    """仅用于日志：判断一段文本是否声称是检索报告但解析失败。"""
    return isinstance(text, str) and REPORT_PROTOCOL in text


def _collect_retrieval_records(messages: Sequence[BaseMessage]) -> list[tuple[str, dict]]:
    records = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        artifact = message.artifact
        if not isinstance(artifact, dict):
            continue
        if artifact.get("protocol") != RETRIEVAL_PROTOCOL:
            continue
        if artifact.get("version") != PROTOCOL_VERSION:
            continue
        records.append((message.tool_call_id, artifact))
    return records


def _final_ai_text(messages: Sequence[BaseMessage]) -> str:
    """只取最后一条 AI 消息的文本。

    若最后一条是工具消息或没有正文的 AI 消息，说明子代理没有给出最终回答；
    此时不能回退到检索前的中间叙述（那可能只是"我来查一下"之类的过程话术）。
    """
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message.text.strip()
        if isinstance(message, ToolMessage):
            return ""
    return ""


def _json_object(text: Any) -> dict | None:
    if not isinstance(text, str):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)
