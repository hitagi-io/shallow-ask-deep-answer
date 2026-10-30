import os
import threading
from datetime import date

from langchain.agents.middleware import SummarizationMiddleware, dynamic_prompt
from langchain.chat_models import init_chat_model
from deepagents import create_deep_agent
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool
from utils.answer_guard import (
    SEARCH_EMPTY_MARKER,
    SEARCH_ERROR_MARKER,
    SENTINEL_KB_MISS,
    guarded_stream,
)
from utils.config_loader import agent_config
from utils.pg_lock import PG_LOCK_KEY_SETUP, exclusive_section
from utils.prompt_loader import render_general_prompt, render_main_prompt
from rag.rag_summary import rag_summary_subagents
from langchain_tavily import TavilySearch
from langchain_core.tools import tool
from dotenv import load_dotenv
load_dotenv()

# Postgres 连接串（与 auth.py 保持一致，复用同一 PostgreSQL 实例）
_PG_CONNECTION_STRING = os.getenv(
    "PG_CONNECTION_STRING", "postgresql://postgres:postgres@localhost:5432/deepagent_rag"
)

# 检查点连接池大小（本进程共享）：检查点是"每次读写借还一次连接"的短操作，
# 池上限只需覆盖同时并发执行的检查点操作数，不必等于并发流数。
# 多 worker 部署时注意 workers × (本池上限 + 业务池上限) 不能超过 PG max_connections。
PG_CHECKPOINT_POOL_MIN = int(os.getenv("PG_CHECKPOINT_POOL_MIN", "2"))
PG_CHECKPOINT_POOL_MAX = int(os.getenv("PG_CHECKPOINT_POOL_MAX", "20"))

_tavily: TavilySearch | None = None
_tavily_lock: threading.Lock = threading.Lock()

# 「仅知识库」模式必须堵住 deepagents 默认注入的 general-purpose 子代理：
# 它没有检索能力却可能用自身知识作答，会破坏"零训练知识"这条约束。
# deepagents 的规则是：subagents 中出现同名 spec 时不再追加默认项，因此用同名覆盖它，
# 使其唯一的"知识类"输出只能是固定短句（降级回答兜底会把该短句判为知识库无资料并接管文案）。
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
        # 异常原文只写服务端日志：工具结果会被传入模型，既可能被当成正文，
        # 也会把上游服务的地址/鉴权等细节带进模型上下文。
        # 返回标记而非"没有结果"的措辞，让模型能区分故障与无果
        print(f"[联网搜索] 搜索失败，已向模型返回故障标记: {e}")
        return SEARCH_ERROR_MARKER
    results = resp.get("results") or []
    answer = (resp.get("answer") or "").strip()
    if not results:
        # 服务正常但没有任何结果：独立标记，与"服务故障"严格区分
        return f"总结：{answer}" if answer else SEARCH_EMPTY_MARKER
    return f"总结：{answer}\n" + "\n".join(
        f"{i}. {r['title']}\n   {r['url']}"
        for i, r in enumerate(results, 1)
    )


class MainDeepAgent:
    def __init__(self):
        self.model=init_chat_model(
            model=agent_config["main_agent_model"],
            model_provider=agent_config["main_agent_model_provider"],
            base_url=agent_config["main_agent_model_base_url"],
        )
        # 共享同一个 checkpointer，保证「仅知识库」开关切换时对话上下文不丢失
        # 对话记录持久化到 PostgreSQL（LangGraph 检查点），重启后仍可恢复上下文
        self._pg_pool = ConnectionPool(
            conninfo=_PG_CONNECTION_STRING,
            min_size=PG_CHECKPOINT_POOL_MIN,
            max_size=PG_CHECKPOINT_POOL_MAX,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        self.checkpointer = PostgresSaver(self._pg_pool)
        # 多 worker 同时启动会并发执行建表 DDL 而冲突，用 PG 咨询锁串行化；
        # 表已存在时 setup 极快，后续 worker 依次通过
        with exclusive_section(_PG_CONNECTION_STRING, PG_LOCK_KEY_SETUP):
            self.checkpointer.setup()
        self.kb_only_prompt = render_main_prompt()
        self.general_prompt = render_general_prompt()
        # 预构建两个 agent：默认「仅知识库」，关闭后为通用模式（自身知识 + 联网搜索）
        self._kb_only_agent = self._build_kb_only_agent()
        self._general_agent = self._build_general_agent()

    def _middleware(self, static_prompt: str):
        # 摘要压缩：达到任一阈值即触发（OR 语义），摘要后保留最近 10 条消息，
        # 防止长对话无限增长超出上下文上限、检查点随之外膨胀
        return [
            SummarizationMiddleware(
                model=self.model,
                trigger=[("messages", 30), ("tokens", 20000)],
                keep=("messages", 10),
            ),
            # 动态注入当天日期：构建期 prompt 是静态的，跨天运行不能依赖启动快照
            build_date_prompt_middleware(static_prompt),
        ]

    def _build_kb_only_agent(self):
        # create_deep_agent 会额外注入默认工具集（write_todos、filesystem 工具、execute、task）。
        # 默认后端是 StateBackend（内存态虚拟文件系统），execute 在非沙箱后端下只返回错误信息，
        # 因此即便文档或网页携带提示注入，也读不到本机文件、执行不了命令。
        # 若将来改用 FilesystemBackend / LocalShellBackend，必须先评估提示注入导致读写本地文件的风险。
        return create_deep_agent(
            model=self.model,
            tools=[],
            system_prompt=self.kb_only_prompt,
            checkpointer=self.checkpointer,
            # 严格模式不含联网工具，无 tavily：零训练知识、零联网，仅知识库
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

    def ask_deep_agent(self, query, thread_id, kb_only=True):
        agent = self._kb_only_agent if kb_only else self._general_agent
        raw_stream = agent.stream(
            {"messages": [{"role": "human", "content": query}]},
            config={"configurable": {"thread_id": thread_id}},
            stream_mode="messages",
        )
        # 工具消息不返回给前端：严格模式先经兜底模块解析用于回合判定，
        # 降级回合由兜底模块直接产出固定文案并提前终止底层流；
        # 通用模式只过滤工具消息，模型文本实时透传
        yield from guarded_stream(raw_stream, kb_only=kb_only)