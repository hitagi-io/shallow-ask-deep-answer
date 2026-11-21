"""运行知识库问答，并把完成校验的对话保存到跨轮检查点。"""

from collections.abc import Callable
from contextlib import closing
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.graph.state import CompiledStateGraph

from utils.chat.answer_guard import guarded_stream


def stream_kb_turn(agent: CompiledStateGraph, query: str, config: RunnableConfig,
                   should_stop: Callable[[], bool] | None):
    history = agent.get_state(config).values.get("messages", [])
    question = HumanMessage(content=query, id=uuid4().hex)
    state_messages = []

    def model_events():
        nonlocal state_messages
        # 工具消息、草稿及其摘要仅在本次执行中存在，取消或异常时也不会成为下一轮输入。
        worker = agent.copy(update={"checkpointer": False})
        with closing(worker.stream({"messages": [*history, question]}, config=config,
                                   stream_mode=["messages", "values"])) as events:
            for mode, value in events:
                if mode == "values":
                    state_messages = value["messages"]
                else:
                    yield value

    def save_answer(answer: str) -> None:
        # 只复用问题之前的摘要。若本轮问题也已被压缩，摘要可能含有草稿，保留执行前的历史。
        prefix = history
        for index, message in enumerate(state_messages):
            if message.id == question.id:
                prefix = state_messages[:index]
                break
        agent.update_state(config, {
            "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *prefix, question, AIMessage(content=answer)],
        }, as_node="model")

    yield from guarded_stream(model_events(), kb_only=True, should_stop=should_stop, on_final_text=save_answer)
