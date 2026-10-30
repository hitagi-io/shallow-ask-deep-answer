"""降级回答兜底：裁决"仅知识库"模式的流式输出，接管降级文案；通用模式仅过滤工具消息。

两种模式（与前端「仅知识库」开关一一对应）：
- kb_only=True（默认，严格模式）：事实性内容只能来自知识库。子代理用固定短句报告
  "无资料 / 服务故障"，本模块在代码层解析并通过固定文案接管；模型文本先缓冲，
  只有检索成功才放行，降级时直接输出固定文案并提前终止底层流。
- kb_only=False（通用模式）：模型自身知识 + 必要时联网搜索，没有固定文案约定；
  本模块只过滤工具消息（并记录联网异常标记），模型文本实时透传。

固定文案与固定短句以本模块为唯一来源，提示词模板经 utils.prompt_loader 注入，
避免两处不一致。
"""

# ---- 工具层标记串（模型在工具输出中看到，提示词据此解释含义）----
RETRIEVAL_ERROR_MARKER = "__RETRIEVAL_UNAVAILABLE__"  # do_retrieval 检索服务故障
SEARCH_ERROR_MARKER = "__SEARCH_UNAVAILABLE__"        # tavily_search 搜索服务故障
SEARCH_EMPTY_MARKER = "__SEARCH_EMPTY__"              # tavily_search 正常但没有结果

# ---- 子代理固定短句（作为约定：整段唯一内容，代码精确解析）----
SENTINEL_KB_DOWN = "检索服务暂时不可用"  # 子代理：检索工具故障
SENTINEL_KB_MISS = "未找到足够资料"      # 子代理：知识库无资料

# ---- 用户可见固定文案（唯一来源，渲染进提示词模板）----
COPY_KB_DOWN = "抱歉，知识库检索服务暂时不可用，请稍后重试。"
COPY_KB_MISS = "抱歉，内部知识库中未找到相关资料。您可以关闭「仅知识库」开关后重试。"

# 子代理成功输出的来源标注前缀（仅用于日志检查，不参与判定）
_SOURCE_BLOCK_PREFIX = "来源："

# 降级获胜状态 -> 用户可见固定文案
_COPY_BY_WINNER = {
    "kb_down": COPY_KB_DOWN,
    "kb_miss": COPY_KB_MISS,
}


class AnswerGuard:
    """仅知识库模式的单回合输出判定器：feed_tool / feed_text / finish 三个入口。

    调用方（guarded_stream）负责从流中取出消息并区分工具消息与文本消息；
    本类只维护回合状态，返回"此刻应发给用户的内容"（None 表示继续缓冲）。
    """

    def __init__(self) -> None:
        self.terminated = False  # 已输出降级文案并终止回合
        self._released = False   # 缓冲文本已放行，后续文本实时透传
        self._buffer: list[str] = []
        self._kb_success = False
        self._kb_down = False
        self._kb_miss = False
        self._seen_tools: list[str] = []  # 仅用于日志（零检索回合排查）

    # ---------- 状态入口 ----------

    def feed_tool(self, text: str, tool_name: str | None = None) -> str | None:
        """处理一条工具消息，返回此刻需要直接发给用户的文本（放行缓冲或降级文案）。"""
        if self.terminated:
            return None
        text = text or ""
        if tool_name:
            self._seen_tools.append(tool_name)

        # 防御：子代理内部工具标记意外进入主流时，同样按知识库故障处理
        if RETRIEVAL_ERROR_MARKER in text:
            self._kb_down = True
            return self._settle("知识库检索服务故障")

        if tool_name == "task":
            if SENTINEL_KB_DOWN in text:
                self._kb_down = True
                self._log_if_nonstandard(text, SENTINEL_KB_DOWN)
                return self._settle("知识库检索服务故障")
            if SENTINEL_KB_MISS in text:
                self._kb_miss = True
                self._log_if_nonstandard(text, SENTINEL_KB_MISS)
                return self._settle("知识库无资料")
            self._kb_success = True
            if _SOURCE_BLOCK_PREFIX not in text:
                print("[兜底] 知识库检索成功，但子代理输出缺少「来源：」标注，请检查检索提示词")
            return self._settle("知识库检索成功")

        return None

    def feed_text(self, text: str) -> str | None:
        """处理一段模型文本：已放行则透传，否则缓冲。"""
        if self.terminated or not text:
            return None
        if self._released:
            return text
        self._buffer.append(text)
        return None

    def finish(self) -> str | None:
        """回合结束（流耗尽）时的收尾：放行缓冲或输出兜底固定文案。"""
        if self.terminated:
            return None
        winner = self._winner()
        if winner is None:
            # 无工具调用属正常直答（如寒暄），不打印；调用了工具却未走知识库检索才值得记录，
            # 便于排查提示词或工具路由问题
            if self._seen_tools:
                tools = "、".join(self._seen_tools)
                print(f"[兜底] 本回合未出现知识库检索，文本直接放行（已见工具：{tools}）")
            self._released = True
            return self._flush()
        if winner == "kb_success":
            self._released = True
            return self._flush()
        # kb_down / kb_miss 理论上已在 feed_tool 中终止，此处兜底同样接管
        return self._terminate(winner, "回合结束兜底接管")

    # ---------- 内部逻辑 ----------

    def _winner(self) -> str | None:
        if self._kb_success:
            return "kb_success"
        if self._kb_down:
            return "kb_down"
        if self._kb_miss:
            return "kb_miss"
        return None

    def _settle(self, reason: str) -> str | None:
        winner = self._winner()
        if winner is None:
            return None
        if winner == "kb_success":
            if self._released:
                return None
            self._released = True
            return self._flush()
        return self._terminate(winner, reason)

    def _terminate(self, winner: str, reason: str) -> str:
        dropped = len("".join(self._buffer))
        self._buffer.clear()
        self.terminated = True
        print(f"[兜底] {reason}，已输出固定文案并终止本回合（丢弃模型文本 {dropped} 字）")
        return _COPY_BY_WINNER[winner]

    def _flush(self) -> str | None:
        out = "".join(self._buffer)
        self._buffer.clear()
        return out or None

    @staticmethod
    def _log_if_nonstandard(text: str, sentinel: str) -> None:
        if text.strip() != sentinel:
            preview = text.strip().replace("\n", " ")[:60]
            print(f"[兜底] 子代理固定短句包含附加文字，请检查检索提示词：{preview}")


# ---------- 流包装 ----------

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


def _close_stream(stream) -> None:
    """提前终止时关闭底层流，避免继续消耗模型调用。"""
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception as e:
            print(f"[兜底] 关闭底层流失败（已忽略）: {e}")


def _general_stream(raw_stream):
    """通用模式：不做文案接管，仅过滤工具消息。"""
    for item in raw_stream:
        chunk = item[0] if isinstance(item, (tuple, list)) and len(item) == 2 else item
        text = _chunk_text(chunk)
        # 联网异常标记由 tavily_search 记日志，这里只负责过滤工具消息，避免重复刷屏
        if getattr(chunk, "type", "") == "tool":
            continue
        if text:
            yield text


def guarded_stream(raw_stream, kb_only: bool = True):
    """把 Agent 的原始消息流包装为"用户可见文本流"。

    输入为 (chunk, metadata) 二元组或消息块本身（LangGraph stream_mode="messages" 的形态）；
    工具消息不产出文本：严格模式下参与回合判定，通用模式下直接过滤。
    严格模式的降级回合由本函数直接产出固定文案并提前终止底层流。
    """
    if not kb_only:
        yield from _general_stream(raw_stream)
        return

    guard = AnswerGuard()
    for item in raw_stream:
        chunk = item[0] if isinstance(item, (tuple, list)) and len(item) == 2 else item
        text = _chunk_text(chunk)
        if getattr(chunk, "type", "") == "tool":
            emit = guard.feed_tool(text, getattr(chunk, "name", None))
            if emit:
                yield emit
            if guard.terminated:
                break
            continue
        emit = guard.feed_text(text)
        if emit:
            yield emit
    if guard.terminated:
        _close_stream(raw_stream)
        return
    emit = guard.finish()
    if emit:
        yield emit
