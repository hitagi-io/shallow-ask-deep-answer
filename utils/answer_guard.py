"""降级回答兜底：裁决"仅知识库"模式的流式输出，接管降级文案；通用模式仅过滤工具消息。

两种模式（与前端「仅知识库」开关一一对应）：
- kb_only=True（默认，严格模式）：事实性内容只能来自知识库。子代理在出口由代码根据
  检索 artifact 生成结构化报告；本模块只认结构报告，不扫描任何失败短句。模型文本
  先缓冲，拿到 status=ok 且来源可校验的回合才增量放行；回合结束时再校验引用，
  校验失败则用 replace 事件用固定文案整段替换已下发内容。
- kb_only=False（通用模式）：模型自身知识 + 必要时联网搜索，没有固定文案约定；
  本模块只过滤工具消息（并记录联网异常标记），模型文本实时透传。

固定文案以本模块为唯一来源，提示词模板经 utils.prompt_loader 注入。
"""

from utils.retrieval_protocol import (
    CITATION_SOURCE_LIMIT,
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    looks_like_report,
    parse_direct_answer_content,
    parse_retrieval_report,
    split_answer_and_citations,
)

# 通用模式向模型传递的两种联网搜索状态。
SEARCH_ERROR_MARKER = "__SEARCH_UNAVAILABLE__"
SEARCH_EMPTY_MARKER = "__SEARCH_EMPTY__"

COPY_KB_DOWN = "抱歉，知识库检索服务暂时不可用，请稍后重试。"
COPY_KB_MISS = "抱歉，内部知识库中未找到相关资料。您可以关闭「仅知识库」开关后重试。"
COPY_KB_UNVERIFIED = "抱歉，本次回答未能通过知识库依据校验，请稍后重试。"

_COPY_BY_WINNER = {
    "kb_down": COPY_KB_DOWN,
    "kb_miss": COPY_KB_MISS,
    "unverified": COPY_KB_UNVERIFIED,
}
_FIXED_COPY_TEXTS = frozenset(_COPY_BY_WINNER.values())


class AnswerGuard:
    """仅知识库模式的单回合输出判定器。

    调用方（guarded_stream）负责从流中取出消息并区分工具消息与文本消息；
    本类维护回合状态：拿到 status=ok 报告后允许增量放行，finish() 返回最终
    校验动作（追加来源行 / 整段替换 / 无输出）。
    """

    def __init__(self) -> None:
        self.terminated = False
        self._buffer: list[str] = []
        self._reports: list[dict] = []
        self._direct_category: str | None = None
        self._task_seen = False
        self._task_call_ids: set[str] = set()
        self._direct_call_ids: set[str] = set()
        self._seen_tools: list[str] = []
        # status=ok 报告到达后进入增量放行：后续文本边生成边下发
        self._releasing = False


    def note_tool_call(self, tool_name: str | None, tool_call_id: str | None = None) -> None:
        """记录模型发起的工具调用，用于把工具结果关联到具体调用。"""
        if tool_name == "task":
            self._task_seen = True
            if tool_call_id:
                self._task_call_ids.add(tool_call_id)
        elif tool_name == "mark_direct_answer" and tool_call_id:
            self._direct_call_ids.add(tool_call_id)

    def begin_release(self) -> str | None:
        """进入增量放行；返回此前已缓冲、需要一次性补发的文本。"""
        if self.terminated or self._releasing:
            return None
        self._releasing = True
        pending = self._buffer_text()
        return pending or None

    def feed_tool(
        self,
        text: str,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> str | None:
        """处理一条工具消息：只接受结构化报告/直答声明，不解析失败短句。

        返回 None（无状态变化）、"generating"（可进入生成阶段）或
        "release"（拿到 status=ok 报告，可开始增量放行）。
        """
        if self.terminated:
            return
        text = text or ""
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
                print("[兜底] 结构化检索报告没有匹配的 task 调用，已忽略")
                return None
            report["task_call_id"] = tool_call_id
            self._reports.append(report)
            sources = "、".join(report["sources"][:CITATION_SOURCE_LIMIT]) or "-"
            print(
                f"[兜底] 结构化检索报告 status={report['status']} "
                f"reason={report.get('reason') or '-'} sources={sources} "
                f"task_call={tool_call_id or '-'}"
            )
            return "release" if report["status"] == STATUS_OK else "generating"

        if looks_like_report(text):
            print("[兜底] 结构化检索报告解析失败，本回合按无有效证据处理")

        category = parse_direct_answer_content(text)
        if category is not None:
            if (
                not tool_call_id
                or tool_call_id not in self._direct_call_ids
                or tool_name not in (None, "mark_direct_answer")
            ):
                print("[兜底] 直答声明没有匹配的 mark_direct_answer 调用，已忽略")
                return None
            self._direct_category = category
            print(f"[兜底] 已声明直答分支 category={category}")
            return "generating"
        return None

    def feed_text(self, text: str) -> str | None:
        """缓冲模型文本；已进入增量放行时返回应立刻下发的增量。"""
        if self.terminated or not text:
            return None
        self._buffer.append(text)
        return text if self._releasing else None

    def finish(self) -> tuple[str, str]:
        """回合结束（流耗尽）时的唯一放行点。

        返回 (action, text)：
        - ("append", suffix)：在已下发内容后追加后缀（如补发的来源行）；
        - ("replace", text)：整段替换为 text（固定文案或完整回答）；
        - ("none", "")：无可输出。
        """
        if self.terminated:
            return ("none", "")

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

        if self._direct_category is not None and not self._task_seen:
            return self._finish_direct()

        if self._buffer_text().strip():
            if self._direct_category is not None:
                return self._terminate("unverified", "已声明直答但本轮仍调用了 task")
            tools = f"（已见工具：{'、'.join(self._seen_tools)}）" if self._seen_tools else ""
            return self._terminate("unverified", f"无检索证据且未声明可直接回答{tools}")
        return ("none", "")


    def _finish_with_evidence(self, ok_reports: list[dict]) -> tuple[str, str]:
        text = self._buffer_text()
        if not text.strip():
            return self._terminate("unverified", "检索成功但本轮没有可校验的回答文本")
        if text.strip() in _FIXED_COPY_TEXTS:
            return self._terminate("unverified", "检索成功但模型输出了降级文案")

        body, cited = split_answer_and_citations(text)
        if not body.strip():
            return self._terminate("unverified", "回答只有来源标注，没有正文")
        if body.strip() in _FIXED_COPY_TEXTS:
            return self._terminate("unverified", "检索成功但模型输出了降级文案")

        sources = _merge_sources(ok_reports)
        invalid = [name for name in cited if name not in sources]
        if invalid:
            return self._terminate(
                "unverified", f"回答引用了本轮检索证据之外的文件：{'、'.join(invalid)}"
            )

        self._buffer.clear()
        if cited:
            return ("replace", text)
        suffix = (
            "\n\n参考来源（内部知识库）："
            f"{'、'.join(sources[:CITATION_SOURCE_LIMIT])}"
        )
        # 已增量下发的正文保持不变，只把缺失的来源行追加到末尾
        return ("append", suffix) if self._releasing else ("replace", f"{body}{suffix}")

    def _finish_direct(self) -> tuple[str, str]:
        text = self._buffer_text()
        if not text.strip():
            return ("none", "")
        _, cited = split_answer_and_citations(text)
        if cited:
            return self._terminate("unverified", "直答分支不应出现来源标注")
        self._buffer.clear()
        return ("replace", text)

    def _buffer_text(self) -> str:
        return "".join(self._buffer)

    def _terminate(self, winner: str, reason: str) -> tuple[str, str]:
        dropped = len(self._buffer_text())
        self._buffer.clear()
        self.terminated = True
        print(f"[兜底] {reason}，已输出固定文案并拦截模型文本 {dropped} 字")
        return ("replace", _COPY_BY_WINNER[winner])



def _chunk_text(chunk) -> str:
    """提取消息块文本，兼容 str 与内容块列表两种 content 形态。"""
    content = getattr(chunk, "content", "")
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


def _tool_calls(chunk) -> list[tuple[str | None, str | None]]:
    """从流式 AI 消息中提取（工具名, tool_call_id），用于关联具体工具调用。"""
    calls = []
    for item in getattr(chunk, "tool_call_chunks", None) or []:
        if isinstance(item, dict):
            calls.append((item.get("name"), item.get("id")))
        else:
            calls.append((getattr(item, "name", None), getattr(item, "id", None)))
    try:
        tool_calls = getattr(chunk, "tool_calls", None) or []
    except Exception:
        tool_calls = []
    for item in tool_calls:
        if isinstance(item, dict):
            calls.append((item.get("name"), item.get("id")))
        else:
            calls.append((getattr(item, "name", None), getattr(item, "id", None)))
    return [
        (str(name) if name else None, str(call_id) if call_id else None)
        for name, call_id in calls
    ]


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

    for item in raw_stream:
        if should_stop is not None and should_stop():
            return
        chunk = item[0] if isinstance(item, (tuple, list)) and len(item) == 2 else item

        next_phase = None
        for name, _ in _tool_calls(chunk):
            if name:
                next_phase = "searching" if name == "tavily_search" else "working"

        if getattr(chunk, "type", "") == "tool":
            event = phase_event("generating")
            if event:
                yield event
            continue

        text = _chunk_text(chunk)
        if text:
            yield text
        if next_phase:
            event = phase_event(next_phase)
            if event:
                yield event


def guarded_stream(raw_stream, kb_only: bool = True, should_stop=None):
    """把 Agent 的原始消息流包装为"用户可见文本流"。

    严格模式先缓冲模型文本；拿到 status=ok 报告后进入增量放行（边生成边下发），
    回合结束时校验来源引用：无引用时补发"参考来源"行，校验失败则下发 replace
    事件用固定文案整段替换已下发内容。没有有效证据的回合始终只输出固定文案。

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

        for item in raw_stream:
            if should_stop is not None and should_stop():
                return
            chunk = item[0] if isinstance(item, (tuple, list)) and len(item) == 2 else item

            next_phase = None
            for name, call_id in _tool_calls(chunk):
                guard.note_tool_call(name, call_id)
                if name == "task":
                    next_phase = "retrieving"

            if getattr(chunk, "type", "") == "tool":
                hint = guard.feed_tool(
                    _chunk_text(chunk),
                    getattr(chunk, "name", None),
                    getattr(chunk, "tool_call_id", None),
                )
                if hint:
                    next_phase = "generating"
                    event = phase_event(next_phase)
                    if event:
                        yield event
                    if hint == "release":
                        pending = guard.begin_release()
                        if pending:
                            emitted.append(pending)
                            yield pending
                continue

            delta = guard.feed_text(_chunk_text(chunk))
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
        if action == "append":
            yield text
        elif not emitted_text:
            yield text
        elif emitted_text != text:
            # 已下发内容与最终校验结果不一致（引用越界、降级文案等），整段替换
            yield {"type": "replace", "content": text}
    finally:
        close = getattr(raw_stream, "close", None)
        if close is not None:
            try:
                close()
            except Exception as e:
                print(f"[兜底] 关闭上游消息流失败（已忽略）: {e}")


def _merge_sources(reports: list[dict]) -> list[str]:
    sources = []
    for report in reports:
        for source in report["sources"]:
            if source not in sources:
                sources.append(source)
    return sources
