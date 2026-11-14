"""跨异步任务及线程传递的关联信息；不包含正文或凭证。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from collections.abc import Callable, Iterator
import time


@dataclass(frozen=True)
class ExecutionContext:
    http_request_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    user_id: int | None = None
    project_id: str | None = None
    generation_id: int | None = None
    attempt_id: str | None = None
    deadline: float | None = None
    should_stop: Callable[[], bool] | None = None


current_context: ContextVar[ExecutionContext] = ContextVar("execution_context", default=ExecutionContext())


class ExecutionStopped(RuntimeError):
    """用户取消或执行器已经结束等待。"""


class ExecutionDeadlineExceeded(TimeoutError):
    """生成总时限已到，不能转换为工具故障后继续请求模型。"""


@contextmanager
def bind_context(context: ExecutionContext) -> Iterator[None]:
    token = current_context.set(context)
    try:
        yield
    finally:
        current_context.reset(token)


@contextmanager
def execution_deadline(seconds: float, should_stop: Callable[[], bool]) -> Iterator[None]:
    with bind_context(replace(current_context.get(), deadline=time.monotonic() + seconds, should_stop=should_stop)):
        yield


def remaining_seconds() -> float | None:
    context = current_context.get()
    if context.should_stop is not None and context.should_stop():
        raise ExecutionStopped("执行已停止")
    if context.deadline is None:
        return None
    remaining = context.deadline - time.monotonic()
    if remaining <= 0:
        raise ExecutionDeadlineExceeded("生成总时限已到")
    return remaining
