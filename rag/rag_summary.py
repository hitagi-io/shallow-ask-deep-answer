import os

from deepagents import CompiledSubAgent
from langchain.chat_models import init_chat_model
from langchain.tools import tool
from langchain_core.runnables import RunnableLambda
from utils.retrieval_protocol import (
    RETRIEVAL_EMPTY_MARKER,
    RETRIEVAL_ERROR_MARKER,
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    RetrievalReport,
    build_report_from_messages,
    build_retrieval_artifact,
)
from utils.prompt_loader import render_summary_prompt
from rag.vector_store import get_vector_store
from utils.config_loader import agent_config
from langchain.agents import create_agent
from dotenv import load_dotenv
load_dotenv()



@tool(response_format="content_and_artifact")
def do_retrieval(query):
    """从内部知识库检索与用户问题相关的专业文档和资料。

    优先使用此工具查询：概念解释、术语定义、专业知识、内部文档内容。

    Args:
        query: 改写后的自包含问题，或需要检索的关键词。

    Returns:
        tuple[str, dict]: 文本结果给模型阅读（每项以「【来源：文件名】」开头）；
        结构化 artifact 供系统判定检索状态（ok / empty / error），其中 error
        与 empty 严格区分。
    """
    if not isinstance(query, str) or not query.strip():
        return RETRIEVAL_EMPTY_MARKER, build_retrieval_artifact(STATUS_EMPTY, "", [])
    try:
        retriever = get_vector_store().get_retriever()
        docs = retriever.invoke(query)
    except Exception as e:
        print(f"[知识库检索] 检索失败，已向子代理返回故障标记: {e}")
        return RETRIEVAL_ERROR_MARKER, build_retrieval_artifact(STATUS_ERROR, query, [])

    chunks = []
    for doc in docs:
        source = os.path.basename(str(doc.metadata.get("source", "未知来源")))
        content = str(doc.page_content or "")
        if content.strip():
            chunks.append((source, content))

    if not chunks:
        return RETRIEVAL_EMPTY_MARKER, build_retrieval_artifact(STATUS_EMPTY, query, [])
    text = "\n\n".join(f"【来源：{source}】\n{content}" for source, content in chunks)
    return text, build_retrieval_artifact(STATUS_OK, query, chunks)


rag_summary_agent = create_agent(
    model=init_chat_model(
        model=agent_config["rag_summary_model"],
        model_provider=agent_config["rag_summary_model_provider"],
        base_url=agent_config["rag_summary_model_base_url"],
        timeout=float(agent_config.get("llm_request_timeout_seconds", 120)),
        max_retries=int(agent_config.get("llm_max_retries", 1)),
    ),
    tools=[do_retrieval],
    system_prompt=render_summary_prompt(),
)


def _invoke_with_report(state):
    """运行子代理，并用代码（而非模型文本）生成结构化检索报告。"""
    result = rag_summary_agent.invoke(state)
    if isinstance(result, dict) and isinstance(result.get("messages"), list):
        result = dict(result)
        result["structured_response"] = RetrievalReport(
            **build_report_from_messages(result["messages"])
        )
    return result


structured_rag_summary_agent = RunnableLambda(_invoke_with_report)

rag_summary_subagents = CompiledSubAgent(
    name="rag_summary_subagents",
    description="知识库检索专家。专门用于从内部向量数据库中检索专业文档、技术资料和知识库内容。当用户询问任何需要查阅内部资料的问题时（如特定概念解释、文档查询、专业知识等），必须首先调用此子代理。它会自动检索相关文档并生成基于知识库的准确回答。",
    runnable=structured_rag_summary_agent,
)
