"""回答输出校验：裁决“仅知识库”模式的流式输出，并处理降级响应；通用模式仅过滤工具消息。

两种模式（与前端的问答模式选择一一对应）：
- kb_only=True（默认，严格模式）：事实性内容只能来自知识库。子代理在出口由代码根据
  检索 artifact 生成结构化报告；本模块只认结构报告，不扫描任何失败短句。模型文本
  先缓冲，当前回合收齐任务报告且至少一份 status=ok 后才增量放行；回合结束时再校验引用，
  校验失败则用 replace 事件用固定文案整段替换已下发内容。
- kb_only=False（通用模式）：模型自身知识 + 必要时联网搜索，没有固定文案约定；
  本模块只过滤工具消息（并记录联网异常标记），模型文本实时透传。

固定文案以本模块为唯一来源，提示词模板经 utils.prompt_loader 注入。
"""

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage

from utils.retrieval_protocol import (
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    looks_like_report,
    parse_clarification_content,
    parse_retrieval_report,
)
from utils.logging_config import get_logger
from utils.citations import cited_evidence_ids, render_citations

logger = get_logger(__name__)

# 通用模式向模型传递的两种联网搜索状态。
SEARCH_ERROR_MARKER = "__SEARCH_UNAVAILABLE__"
SEARCH_EMPTY_MARKER = "__SEARCH_EMPTY__"

COPY_KB_DOWN = "抱歉，知识库暂时无法查询，请稍后再试。"
COPY_KB_NO_ANSWER = "抱歉，本次未能根据知识库资料给出可靠回答。您可以补充问题描述后重试。"
# 用户侧统一提示；内部状态和日志仍区分无资料与校验未通过。
COPY_KB_MISS = COPY_KB_NO_ANSWER
COPY_KB_UNVERIFIED = COPY_KB_NO_ANSWER

_COPY_BY_WINNER = {
    "kb_down": COPY_KB_DOWN,
    "kb_miss": COPY_KB_MISS,
    "unverified": COPY_KB_UNVERIFIED,
}
_FIXED_COPY_TEXTS = frozenset(_COPY_BY_WINNER.values())


class AnswerGuard:
    """仅知识库模式的单回合输出判定器。

    调用方（guarded_stream）负责从流中取出消息并区分工具消息与文本消息；
    本类维护单回合状态：拿到 status=ok 报告后允许增量放行，finish() 返回最终
    校验动作（生成原文查看链接后整段替换 / 无输出）。
    """

    def __init__(self) -> None:
        self.terminated = False
        self._buffer: list[str] = []
        self._reports: list[dict] = []
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
            if tool_call_id:
                if tool_call_id not in self._task_call_ids:
                    # 检索前的计划或推测不属于最终答案。
                    self._buffer.clear()
                    self._releasing = False
                self._task_call_ids.add(tool_call_id)
        elif tool_name == "ask_kb_clarification" and tool_call_id:
            self._clarification_call_ids.add(tool_call_id)

    def begin_release(self) -> None:
        """全部已派任务返回后放行，丢弃检索完成前的计划和推测。"""
        if self.terminated or self._releasing:
            return None
        self._releasing = True
        self._buffer.clear()

    def feed_tool(
        self,
        text: str,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> str | None:
        """处理一条工具消息：只接受结构化报告/澄清请求，不解析失败短句。

        返回 None（无状态变化）、"generating"（可进入生成阶段）或
        "release"（拿到 status=ok 报告，可开始增量放行）。
        """
        if self.terminated:
            return
        if tool_name:
            self._seen_tools.append(tool_name)
        if tool_name == "task":
            self._task_seen = True

        report = parse_retrieval_report(text)
        if report is not None:
            if (
                not tool_call_id
                or tool_call_id not in self._task_call_ids
                or tool_name not in (None, "task")
            ):
                logger.warning("[输出校验] 结构化检索报告未匹配到对应的 task 调用，已忽略该结果")
                return None
            if any(r["task_call_id"] == tool_call_id for r in self._reports):
                return None
            report["task_call_id"] = tool_call_id
            self._reports.append(report)
            sources = "、".join(report["sources"][:5]) or "-"
            logger.info(
                f"[输出校验] 结构化检索报告 status={report['status']} "
                f"reason={report.get('reason') or '-'} sources={sources} "
                f"task_call={tool_call_id or '-'}"
            )
            complete = {r["task_call_id"] for r in self._reports} == self._task_call_ids
            has_evidence = any(r["status"] == STATUS_OK for r in self._reports)
            return "release" if complete and has_evidence and not self._clarification_call_ids else "generating"

        if looks_like_report(text):
            logger.error("[输出校验] 结构化检索报告解析失败，当前回合按无有效证据处理")

        question = parse_clarification_content(text)
        if question is not None:
            if (
                not tool_call_id
                or tool_call_id not in self._clarification_call_ids
                or tool_name not in (None, "ask_kb_clarification")
            ):
                logger.warning("[输出校验] 澄清结果未匹配到对应工具调用，已忽略该结果")
                return None
            self._clarification = question
            return "generating"
        return None

    def feed_text(self, text: str) -> str | None:
        """缓冲模型文本；已进入增量放行时返回应立刻下发的增量。"""
        if self.terminated or not text:
            return None
        self._buffer.append(text)
        return text if self._releasing else None

    def finish(self) -> tuple[str, str]:
        """流耗尽后执行最终校验，必要时覆盖此前增量放行的内容。

        返回 (action, text)：
        - ("replace", text)：整段替换为 text（固定文案或完整回答）；
        - ("none", "")：无可输出。
        """
        if self.terminated:
            return ("none", "")

        if self._clarification_call_ids and self._task_seen:
            return self._terminate("unverified", "澄清回合不应同时执行检索")
        if self._task_call_ids != {r["task_call_id"] for r in self._reports}:
            return self._terminate("unverified", "部分检索任务缺少有效报告")

        ok_reports = [report for report in self._reports if report["status"] == STATUS_OK]
        if ok_reports:
            return self._finish_with_evidence(ok_reports)

        if any(report["status"] == STATUS_ERROR for report in self._reports):
            return self._terminate("kb_down", "结构化报告为知识库检索服务故障")
        if any(report["status"] == STATUS_EMPTY for report in self._reports):
            return self._terminate("kb_miss", "结构化报告为知识库无资料")
        if self._reports:
            statuses = "、".join(sorted({report["status"] for report in self._reports}))
            return self._terminate("unverified", f"结构化报告状态不可用（{statuses}）")

        if self._clarification is not None and not self._task_seen:
            self._buffer.clear()
            return ("replace", self._clarification)

        if self._buffer_text().strip():
            tools = f"（已见工具：{'、'.join(self._seen_tools)}）" if self._seen_tools else ""
            return self._terminate("unverified", f"无检索证据或有效澄清请求{tools}")
        return ("none", "")


    def _finish_with_evidence(self, ok_reports: list[dict]) -> tuple[str, str]:
        text = self._buffer_text()
        if not text.strip():
            return self._terminate("unverified", "检索成功但本轮没有可校验的回答文本")
        if text.strip() in _FIXED_COPY_TEXTS:
            return self._terminate("unverified", "检索成功但模型输出了降级文案")

        evidence = []
        for report in ok_reports:
            # 主助手只能保留子代理结论已使用的证据，不能把未采用的召回块当作出处。
            used = cited_evidence_ids(report["answer"], report["evidence"])
            evidence.extend(item for item in report["evidence"] if item["citation_id"] in used)
        try:
            result = render_citations(text, evidence)
        except ValueError as exc:
            return self._terminate("unverified", str(exc))
        self._buffer.clear()
        return ("replace", result)

    def _buffer_text(self) -> str:
        return "".join(self._buffer)

    def _terminate(self, winner: str, reason: str) -> tuple[str, str]:
        dropped = len(self._buffer_text())
        self._buffer.clear()
        self.terminated = True
        logger.warning(f"[输出校验] {reason}，已拦截模型生成的 {dropped} 个字符并返回固定文案")
        return ("replace", _COPY_BY_WINNER[winner])



def _tool_calls(chunk: BaseMessage) -> list[tuple[str | None, str | None]]:
    """从流式 AI 消息中提取（工具名, tool_call_id），用于关联具体工具调用。"""
    if isinstance(chunk, AIMessageChunk):
        return [(call["name"], call["id"]) for call in chunk.tool_call_chunks]
    if isinstance(chunk, AIMessage):
        return [(call["name"], call["id"]) for call in chunk.tool_calls]
    return []


def _general_stream(raw_stream, should_stop=None):
    """通用模式：文本实时透传，仅过滤工具消息，并按工具调用下发阶段提示。"""
    phase = ""

    def phase_event(name: str) -> dict | None:
        nonlocal phase
        if name == phase:
            return None
        phase = name
        return {"type": "phase", "phase": name}

    event = phase_event("thinking")
    if event:
        yield event

    for chunk, _metadata in raw_stream:
        if should_stop is not None and should_stop():
            return

        next_phase = None
        for name, _ in _tool_calls(chunk):
            if name:
                next_phase = "searching" if name == "tavily_search" else "working"

        if isinstance(chunk, ToolMessage):
            event = phase_event("generating")
            if event:
                yield event
            continue

        text = chunk.text
        if text:
            yield text
        if next_phase:
            event = phase_event(next_phase)
            if event:
                yield event


def guarded_stream(raw_stream, kb_only: bool = True, should_stop=None):
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

    should_stop 为取消判定（threading.Event.is_set）。kb 模式需要读取上游消息，
    外层无法在读取过程中察觉取消，必须在循环里主动检查，否则停止/断连后仍会把
    整个模型流读完。

    无论正常结束、异常还是调用方 close()，都在 finally 里显式关闭原始流，
    保证取消能继续传播到 LangGraph / 模型请求，而不是只停在外层包装器。
    """
    try:
        if not kb_only:
            yield from _general_stream(raw_stream, should_stop=should_stop)
            return

        guard = AnswerGuard()
        emitted: list[str] = []
        phase = ""

        def phase_event(name: str) -> dict | None:
            nonlocal phase
            if name == phase:
                return None
            phase = name
            return {"type": "phase", "phase": name}

        event = phase_event("thinking")
        if event:
            yield event

        for chunk, _metadata in raw_stream:
            if should_stop is not None and should_stop():
                return

            next_phase = None
            for name, call_id in _tool_calls(chunk):
                guard.note_tool_call(name, call_id)
                if name == "task":
                    next_phase = "retrieving"

            if isinstance(chunk, ToolMessage):
                hint = guard.feed_tool(
                    chunk.text,
                    chunk.name,
                    chunk.tool_call_id,
                )
                if hint:
                    next_phase = "generating"
                    event = phase_event(next_phase)
                    if event:
                        yield event
                    if hint == "release":
                        guard.begin_release()
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
        action, text = guard.finish()
        if action == "none" or not text:
            return

        emitted_text = "".join(emitted)
        if not emitted_text:
            yield text
        elif emitted_text != text:
            # 已下发内容与最终校验结果不一致（引用越界、降级文案等），整段替换
            yield {"type": "replace", "content": text}
    finally:
        try:
            raw_stream.close()
        except Exception as e:
            logger.error(f"[输出校验] 关闭上游消息流失败，继续结束请求：{e}")
