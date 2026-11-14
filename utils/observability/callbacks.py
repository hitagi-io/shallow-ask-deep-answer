"""只采集模型调用元数据；不复制消息、工具参数或模型正文。"""
from uuid import UUID
import threading

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from utils.observability.context import current_context, bind_context, remaining_seconds
from utils.observability.tracing import Span, new_span


class AgentTraceCallback(BaseCallbackHandler):
    run_inline = True
    # 截止时间和取消必须阻止后续模型请求；Trace 存储故障由独立写入器处理。
    raise_error = True

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.context = current_context.get()
        self.active: dict[UUID, Span] = {}
        self.lock = threading.Lock()

    def on_chat_model_start(self, serialized: dict, messages: list[list[BaseMessage]], *,
                            run_id: UUID, parent_run_id: UUID | None = None, **kwargs: object) -> None:
        context = current_context.get()
        if context.trace_id != self.context.trace_id:
            context = self.context
        with bind_context(context):
            remaining_seconds()
        record = new_span("model.call", {"model": self.model_name}, context=context)
        metadata = kwargs.get("metadata")
        if isinstance(metadata, dict):
            model_name = metadata.get("ls_model_name")
            if isinstance(model_name, str):
                record.attributes["model"] = model_name
            source = metadata.get("lc_source")
            if isinstance(source, str):
                record.attributes["source"] = source
        with self.lock:
            self.active[run_id] = record

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: object) -> None:
        with self.lock:
            record = self.active.pop(run_id, None)
        if record is None:
            return
        for generations in response.generations:
            for generation in generations:
                if isinstance(generation, ChatGeneration) and isinstance(generation.message, AIMessage):
                    message = generation.message
                    if message.usage_metadata is not None:
                        record.attributes.update({key: message.usage_metadata[key]
                                                  for key in ("input_tokens", "output_tokens", "total_tokens")})
                    # response_metadata 是供应方开放结构，只有已验证的白名单字段可进入 Trace。
                    for key in ("model_name", "request_id", "finish_reason"):
                        value = message.response_metadata.get(key)
                        if isinstance(value, str):
                            record.attributes[key] = value[:200]
        record.end()

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: object) -> None:
        with self.lock:
            record = self.active.pop(run_id, None)
        if record is not None:
            record.fail(type(error).__name__)
            record.end()

    def close(self) -> None:
        with self.lock:
            unfinished = list(self.active.values())
            self.active.clear()
        for record in unfinished:
            record.status = "cancelled"
            record.end()
