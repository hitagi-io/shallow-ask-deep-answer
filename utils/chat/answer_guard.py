"""回答输出校验：裁决“仅知识库”模式的流式输出，并处理降级响应；通用模式仅过滤工具消息。

两种模式（与前端的问答模式选择一一对应）：
- kb_only=True（默认，严格模式）：事实性内容只能来自知识库。子代理在出口由代码根据
  检索 artifact 生成结构化报告；本模块只认结构报告，不扫描任何失败短句。模型文本
  先缓冲，当前回合收齐任务报告且至少一份 status=ok 后才增量放行；回合结束时再校验引用，
  校验失败则用 replace 事件用固定文案整段替换已下发内容。
- kb_only=False（通用模式）：模型自身知识 + 必要时联网搜索，没有固定文案约定；
  本模块过滤工具消息和内部摘要，只透传用户可见的回答文本。

固定文案以本模块为唯一来源，提示词模板经 utils.chat.prompt_loader 注入。
"""

from collections.abc import Callable

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage

from utils.retrieval.retrieval_protocol import (
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    looks_like_report,
    parse_clarification_content,
    parse_retrieval_report,
)
from utils.core.logging_config import get_logger
from utils.retrieval.citations import CITATION_RE, render_citations
from utils.chat.outcome import ANSWER_REJECTED_ERROR, AnswerOutcome
from utils.observability.tracing import span

logger = get_logger(__name__)

# 通用模式向模型传递的两种联网搜索状态。
SEARCH_ERROR_MARKER = "__SEARCH_UNAVAILABLE__"
SEARCH_EMPTY_MARKER = "__SEARCH_EMPTY__"

COPY_KB_DOWN = "抱歉，知识库暂时无法查询，请稍后再试。"
COPY_KB_MISS = "当前知识库中未检索到足以回答这个问题的资料。请补充具体对象或相关文档。"
COPY_KB_UNVERIFIED = "抱歉，本次未能根据知识库资料给出可靠回答。您可以补充问题描述后重试。"

_COPY_BY_WINNER = {
    "kb_down": COPY_KB_DOWN,
    "kb_miss": COPY_KB_MISS,
    "unverified": COPY_KB_UNVERIFIED,
}
_FIXED_COPY_TEXTS = frozenset(_COPY_BY_WINNER.values())


class AnswerGuard:
    """仅知识库模式的单回合输出判定器。

    调用方（guarded_stream）负责从流中取出消息并区分工具消息与文本消息；
    本类维护单回合状态：收齐任务报告且有证据后允许增量放行，finish() 返回最终正文。
    """

    def __init__(self) -> None:
        self.outcome = AnswerOutcome(result_kind="answer_rejected", error_code="missing_answer")
        self._buffer: list[str] = []
        self._reports: dict[str, dict] = {}
        self._clarification: str | None = None
        self._task_seen = False
        self._task_call_ids: set[str] = set()
        self._clarification_call_ids: set[str] = set()
        self._seen_tools: list[str] = []
        # status=ok 报告到达后进入增量放行：后续文本边生成边下发
        self._releasing = False


    def note_tool_call(self, tool_name: str | None, tool_call_id: str | None = None) -> None:
        """记录模型发起的工具调用，用于把工具结果关联到具体调用。"""
        if tool_name == "task":
            self._task_seen = True
            if tool_call_id and tool_call_id not in self._task_call_ids:
                # 检索前的计划或推测不属于最终答案。
                self._buffer.clear()
                self._releasing = False
                self._task_call_ids.add(tool_call_id)
        elif tool_name == "ask_kb_clarification" and tool_call_id:
            self._clarification_call_ids.add(tool_call_id)

    def feed_tool(
        self,
        text: str,
        tool_name: str | None,
        tool_call_id: str,
    ) -> bool:
        """记录有效的任务或澄清结果，返回本条消息是否进入生成阶段。"""
        if tool_name:
            self._seen_tools.append(tool_name)
        if tool_name == "task":
            self._task_seen = True

        report = parse_retrieval_report(text)
        if report is not None:
            if (
                tool_call_id not in self._task_call_ids
                or tool_name not in (None, "task")
            ):
                logger.warning("[输出校验] 结构化检索报告未匹配到对应的 task 调用，已忽略该结果")
                return False
            if tool_call_id in self._reports:
                return False
            self._reports[tool_call_id] = report
            sources = "、".join(report["sources"][:5]) or "-"
            logger.info(
                f"[输出校验] 结构化检索报告 status={report['status']} "
                f"reason={report['reason'] or '-'} sources={sources} "
                f"task_call={tool_call_id}"
            )
            complete = self._reports.keys() == self._task_call_ids
            has_evidence = any(r["status"] == STATUS_OK for r in self._reports.values())
            if complete and has_evidence and not self._clarification_call_ids:
                self._buffer.clear()
                self._releasing = True
            return True

        if looks_like_report(text):
            logger.error("[输出校验] 结构化检索报告解析失败，当前回合按无有效证据处理")

        question = parse_clarification_content(text)
        if question is not None:
            if (
                tool_call_id not in self._clarification_call_ids
                or tool_name not in (None, "ask_kb_clarification")
            ):
                logger.warning("[输出校验] 澄清结果未匹配到对应工具调用，已忽略该结果")
                return False
            self._clarification = question
            return True
        return False

    def feed_text(self, text: str) -> str | None:
        """缓冲模型文本；已进入增量放行时返回应立刻下发的增量。"""
        if not text:
            return None
        self._buffer.append(text)
        return text if self._releasing else None

    def finish(self) -> str:
        """流耗尽时返回最终正文，空字符串表示没有可输出的文本。"""
        if self._clarification_call_ids and self._task_seen:
            return self._terminate("unverified", "澄清回合不应同时执行检索")
        if self._task_call_ids != self._reports.keys():
            return self._terminate("unverified", "部分检索任务缺少有效报告")

        reports = self._reports.values()
        ok_reports = [report for report in reports if report["status"] == STATUS_OK]
        if ok_reports:
            return self._finish_with_evidence(ok_reports)

        if any(report["status"] == STATUS_ERROR for report in reports):
            return self._terminate("kb_down", "结构化报告为知识库检索服务故障")
        if any(report["status"] == STATUS_EMPTY for report in reports):
            return self._terminate("kb_miss", "结构化报告为知识库无资料")
        if self._reports:
            statuses = "、".join(sorted({report["status"] for report in reports}))
            return self._terminate("unverified", f"结构化报告状态不可用（{statuses}）")

        if self._clarification is not None:
            self._buffer.clear()
            self.outcome = AnswerOutcome(result_kind="clarification")
            return self._clarification

        if self._buffer_text().strip():
            tools = f"（已见工具：{'、'.join(self._seen_tools)}）" if self._seen_tools else ""
            return self._terminate("unverified", f"无检索证据或有效澄清请求{tools}")
        return ""


    def _finish_with_evidence(self, ok_reports: list[dict]) -> str:
        text = self._buffer_text()
        if not text.strip():
            return self._terminate("unverified", "检索成功但本轮没有可校验的回答文本")
        if text.strip() in _FIXED_COPY_TEXTS:
            return self._terminate("unverified", "检索成功但模型输出了降级文案")

        evidence = []
        for report in ok_reports:
            # 报告已通过 parse_retrieval_report 校验，主助手只能沿用其结论引用的片段。
            used = set(CITATION_RE.findall(report["answer"]))
            evidence.extend(item for item in report["evidence"] if item["citation_id"] in used)
        try:
            result, used_ids = render_citations(text, evidence)
        except ValueError as exc:
            return self._terminate("unverified", str(exc))
        with span("answer.citations") as citations:
            citations.attributes["citation_ids"] = sorted(used_ids)
        self._buffer.clear()
        self.outcome = AnswerOutcome(result_kind="answered")
        return result

    def _buffer_text(self) -> str:
        return "".join(self._buffer)

    def _terminate(self, winner: str, reason: str) -> str:
        self.outcome = {
            "kb_down": AnswerOutcome(result_kind="dependency_failed", error_code="retrieval_unavailable"),
            "kb_miss": AnswerOutcome(result_kind="no_evidence"),
            "unverified": AnswerOutcome(result_kind="answer_rejected", error_code="answer_validation_failed"),
        }[winner]
        dropped = sum(map(len, self._buffer))
        self._buffer.clear()
        logger.warning(f"[输出校验] {reason}，已拦截模型生成的 {dropped} 个字符并返回固定文案")
        return _COPY_BY_WINNER[winner]



def _tool_calls(chunk: BaseMessage) -> list[tuple[str | None, str | None]]:
    """从流式 AI 消息中提取（工具名, tool_call_id），用于关联具体工具调用。"""
    if isinstance(chunk, AIMessageChunk):
        return [(call["name"], call["id"]) for call in chunk.tool_call_chunks]
    if isinstance(chunk, AIMessage):
        return [(call["name"], call["id"]) for call in chunk.tool_calls]
    return []


def _is_answer_message(chunk: BaseMessage, metadata: dict) -> bool:
    """messages 流还包含摘要模型输出与状态更新，不能把它们拼进用户答案。"""
    return (
        isinstance(chunk, (AIMessage, ToolMessage))
        and metadata.get("lc_source") != "summarization"
        and metadata.get("langgraph_node") != "SummarizationMiddleware.before_model"
    )


def _general_stream(raw_stream, phase_event: Callable[[str], dict[str, str] | None], should_stop=None):
    """通用模式：只透传回答文本，过滤工具与内部摘要，按工具调用下发阶段提示。"""
    has_text = False
    for chunk, metadata in raw_stream:
        if should_stop is not None and should_stop():
            return
        if not _is_answer_message(chunk, metadata):
            continue

        next_phase = None
        for name, _ in _tool_calls(chunk):
            if name:
                next_phase = "searching" if name == "tavily_search" else "working"

        if isinstance(chunk, ToolMessage):
            if has_text:
                # 工具调用前的说明只是进度；最终答案从工具返回后重新计数。
                yield {"type": "replace", "content": ""}
                has_text = False
            event = phase_event("generating")
            if event:
                yield event
            continue

        text = chunk.text
        if text:
            has_text = has_text or bool(text.strip())
            yield text
        if next_phase:
            event = phase_event(next_phase)
            if event:
                yield event
    if should_stop is None or not should_stop():
        yield AnswerOutcome(result_kind="answered") if has_text else AnswerOutcome(
            result_kind="answer_rejected", error_code="missing_answer")


def guarded_stream(raw_stream, kb_only: bool = True, should_stop=None, *,
                   on_final_text: Callable[[str], None] | None = None):
    """把 Agent 的原始消息流包装为"用户可见文本流"。

    严格模式先缓冲模型文本；收齐任务报告且有 status=ok 时进入增量放行，
    回合结束时校验证据标记，并生成原文页段链接，证据正文按需查看。
    缺少引用或显式引用校验失败时，
    下发 replace 事件，用固定文案整段替换已下发内容。澄清回合只输出工具返回的问句；
    其余没有有效证据的回合只输出固定文案。

    输出元素协议：
    - str：正常文本增量；
    - {"type": "phase", "phase": ...}：阶段提示（thinking / retrieving / generating）；
    - {"type": "replace", "content": ...}：用新内容整段替换此前已下发文本。

    知识库模式在最终结果事件前调用 on_final_text，传入最终正文或空回答的终止提示。

    should_stop 为取消判定（threading.Event.is_set）。kb 模式需要读取上游消息，
    外层无法在读取过程中察觉取消，必须在循环里主动检查，否则停止/断连后仍会把
    整个模型流读完。

    无论正常结束、异常还是调用方 close()，都在 finally 里显式关闭原始流，
    保证取消能继续传播到 LangGraph / 模型请求，而不是只停在外层包装器。
    """
    try:
        phase = "thinking"

        def phase_event(name: str) -> dict[str, str] | None:
            nonlocal phase
            if name == phase:
                return None
            phase = name
            return {"type": "phase", "phase": name}

        yield {"type": "phase", "phase": phase}
        if not kb_only:
            yield from _general_stream(raw_stream, phase_event, should_stop=should_stop)
            return

        guard = AnswerGuard()
        emitted: list[str] = []

        for chunk, metadata in raw_stream:
            if should_stop is not None and should_stop():
                return
            if not _is_answer_message(chunk, metadata):
                continue

            next_phase = None
            for name, call_id in _tool_calls(chunk):
                guard.note_tool_call(name, call_id)
                if name == "task":
                    next_phase = "retrieving"

            if isinstance(chunk, ToolMessage):
                if guard.feed_tool(chunk.text, chunk.name, chunk.tool_call_id):
                    event = phase_event("generating")
                    if event:
                        yield event
                continue

            delta = guard.feed_text(chunk.text)
            if delta:
                emitted.append(delta)
                yield delta
            if next_phase:
                event = phase_event(next_phase)
                if event:
                    yield event

        if should_stop is not None and should_stop():
            return
        with span("answer.validate") as validation:
            text = guard.finish()
            validation.attributes["result_kind"] = guard.outcome.result_kind
            if guard.outcome.error_code is not None:
                validation.fail(guard.outcome.error_code)
        if text:
            emitted_text = "".join(emitted)
            if not emitted_text:
                yield text
            elif emitted_text != text:
                # 已下发内容与最终校验结果不一致（引用越界、降级文案等），整段替换
                yield {"type": "replace", "content": text}
        if should_stop is not None and should_stop():
            return
        if on_final_text is not None:
            on_final_text(text or ANSWER_REJECTED_ERROR)
        yield guard.outcome
    finally:
        try:
            raw_stream.close()
        except Exception as e:
            logger.error(f"[输出校验] 关闭上游消息流失败，继续结束请求：{e}")
