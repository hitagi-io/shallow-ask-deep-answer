import os
import threading
from datetime import date
from typing import Literal

from langchain.agents.middleware import SummarizationMiddleware, dynamic_prompt
from langchain.chat_models import init_chat_model
from deepagents import create_deep_agent
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool
from utils.answer_guard import SEARCH_EMPTY_MARKER, SEARCH_ERROR_MARKER, guarded_stream
from utils.retrieval_protocol import SENTINEL_KB_MISS, build_direct_answer_content
from utils.config_loader import agent_config
from utils.pg_lock import PG_LOCK_KEY_SETUP, exclusive_section
from utils.prompt_loader import render_general_prompt, render_main_prompt
from rag.rag_summary import rag_summary_subagents
from langchain_tavily import TavilySearch
from langchain_core.tools import tool
from dotenv import load_dotenv
load_dotenv()

_PG_CONNECTION_STRING = os.getenv(
    "PG_CONNECTION_STRING", "postgresql://postgres:postgres@localhost:5432/deepagent_rag"
)

PG_CHECKPOINT_POOL_MIN = int(os.getenv("PG_CHECKPOINT_POOL_MIN", "2"))
PG_CHECKPOINT_POOL_MAX = int(os.getenv("PG_CHECKPOINT_POOL_MAX", "20"))

_tavily: TavilySearch | None = None
_tavily_lock: threading.Lock = threading.Lock()

STRICT_GENERAL_PURPOSE_SUBAGENT = {
    "name": "general-purpose",
    "description": (
        "受限辅助子代理：仅处理不依赖外部事实的文本整理类任务。"
        "凡涉及知识、事实、资料的问题，必须交给 rag_summary_subagents 处理。"
    ),
    "system_prompt": (
        "你是一个受限的辅助子代理：没有检索工具，也没有可用的知识来源。"
        f"凡涉及事实、资料、知识的问题，一律只回复「{SENTINEL_KB_MISS}」，"
        "不得使用自身训练知识作答；仅可执行不依赖外部事实的文本整理类任务。"
    ),
    "tools": [],
}


def _inject_current_date(base_prompt: str) -> str:
    """在系统提示末尾追加当天日期（每次模型调用现取，不固化为构建期快照）。"""
    return (
        f"{base_prompt}\n\n## 当前日期\n今天是 {date.today().isoformat()}（服务器本地日期）。"
        "理解「今天/最近/本周」等相对时间以此为准；事实性内容仍以工具检索结果为准。"
    )


def build_date_prompt_middleware(static_prompt: str):
    """构造动态日期中间件。

    deepagents 会把自定义提示词与基础提示词组装成一个完整系统提示随请求传递，
    这里在其末尾追加当天日期：既不改动组装结果（避免覆盖基础提示词），
    又保证进程跨天运行时日期不过期。static_prompt 仅作为兜底。
    """

    def _prompt(request) -> str:
        system_message = getattr(request, "system_message", None)
        content = getattr(system_message, "content", "") if system_message is not None else ""
        base = content if isinstance(content, str) and content else static_prompt
        return _inject_current_date(base)

    return dynamic_prompt(_prompt)


def _get_tavily() -> TavilySearch:
    global _tavily
    if _tavily is None:
        with _tavily_lock:
            if _tavily is None:
                _tavily = TavilySearch(max_results=5, include_answer=True)
    return _tavily


@tool
def tavily_search(query: str) -> str:
    """联网搜索工具，获取实时公开网络信息。

    适用场景：
    - 需要最新动态、实时数据，或对事实没有把握时
    - 涉及公开信息的查询（如新闻、事件、数据）

    Args:
        query: 搜索关键词或自然语言问题。

    Returns:
        str: 格式化的搜索结果，包含 AI 总结和编号的网页标题与链接列表；
        搜索服务异常时返回 SEARCH_ERROR_MARKER，服务正常但没有结果时返回
        SEARCH_EMPTY_MARKER（两者与"有结果"需区分处理）。
    """
    try:
        resp = _get_tavily().invoke(query)
    except Exception as e:
        print(f"[联网搜索] 搜索失败，已向模型返回故障标记: {e}")
        return SEARCH_ERROR_MARKER
    results = resp.get("results") or []
    answer = (resp.get("answer") or "").strip()
    if not results:
        return f"总结：{answer}" if answer else SEARCH_EMPTY_MARKER
    return f"总结：{answer}\n" + "\n".join(
        f"{i}. {r['title']}\n   {r['url']}"
        for i, r in enumerate(results, 1)
    )


@tool
def mark_direct_answer(
    category: Literal["greeting", "identity", "rewrite", "provided_text"],
) -> str:
    """声明本轮属于可直接回答的豁免任务（不依赖外部事实，无需检索）。

    必须在输出回答之前调用，且只允许以下类别：
    - greeting：打招呼、寒暄、客套（如"你好"、"谢谢"）；
    - identity：询问助手身份；
    - rewrite：翻译、改写润色、格式转换；
    - provided_text：对用户本轮消息中已提供文本的摘要、提取、归类。

    涉及知识、事实、资料的问题严禁使用本工具：这类问题必须先调用
    rag_summary_subagents 检索知识库。

    Args:
        category: 豁免任务类别。

    Returns:
        str: 结构化直答声明（服务端据此放行本轮文本，用户不可见）。
    """
    return build_direct_answer_content(category)


class MainDeepAgent:
    def __init__(self):
        self.model=init_chat_model(
            model=agent_config["main_agent_model"],
            model_provider=agent_config["main_agent_model_provider"],
            base_url=agent_config["main_agent_model_base_url"],
            timeout=float(agent_config.get("llm_request_timeout_seconds", 120)),
            max_retries=int(agent_config.get("llm_max_retries", 1)),
        )
        self._pg_pool = ConnectionPool(
            conninfo=_PG_CONNECTION_STRING,
            min_size=PG_CHECKPOINT_POOL_MIN,
            max_size=PG_CHECKPOINT_POOL_MAX,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        self.checkpointer = PostgresSaver(self._pg_pool)
        with exclusive_section(_PG_CONNECTION_STRING, PG_LOCK_KEY_SETUP):
            self.checkpointer.setup()
        self.kb_only_prompt = render_main_prompt()
        self.general_prompt = render_general_prompt()
        self._kb_only_agent = self._build_kb_only_agent()
        self._general_agent = self._build_general_agent()

    def _middleware(self, static_prompt: str):
        return [
            SummarizationMiddleware(
                model=self.model,
                trigger=[("messages", 30), ("tokens", 20000)],
                keep=("messages", 10),
            ),
            build_date_prompt_middleware(static_prompt),
        ]

    def _build_kb_only_agent(self):
        # 严格模式只允许显式直答声明或知识库子代理，不开放通用联网工具。
        return create_deep_agent(
            model=self.model,
            tools=[mark_direct_answer],
            system_prompt=self.kb_only_prompt,
            checkpointer=self.checkpointer,
            subagents=[rag_summary_subagents, STRICT_GENERAL_PURPOSE_SUBAGENT],
            middleware=self._middleware(self.kb_only_prompt),
        )

    def _build_general_agent(self):
        return create_deep_agent(
            model=self.model,
            tools=[tavily_search],
            system_prompt=self.general_prompt,
            checkpointer=self.checkpointer,
            middleware=self._middleware(self.general_prompt),
        )

    def ask_deep_agent(self, query, thread_id, kb_only=True, should_stop=None):
        agent = self._kb_only_agent if kb_only else self._general_agent
        raw_stream = agent.stream(
            {"messages": [{"role": "human", "content": query}]},
            config={"configurable": {"thread_id": thread_id}},
            stream_mode="messages",
        )
        yield from guarded_stream(raw_stream, kb_only=kb_only, should_stop=should_stop)
