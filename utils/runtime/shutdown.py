"""服务关闭时，在可回滚的工作边界通知后台线程停止。"""

import threading
from collections.abc import Callable
from contextlib import contextmanager

from utils.observability.context import remaining_seconds

_stopping = threading.Event()


class ServiceStopping(RuntimeError):
    def __init__(self):
        super().__init__("服务已停止，入库中断，请检查文档状态或重新上传")


def reset_shutdown() -> None:
    _stopping.clear()


def request_shutdown() -> None:
    _stopping.set()


def check_shutdown() -> None:
    if _stopping.is_set():
        raise ServiceStopping()


@contextmanager
def interruptible_lock(lock, *, cancel_check: Callable[[], None] | None = None):
    """等待期间响应服务关闭、当前执行的取消与截止时间。"""
    def check_wait() -> float | None:
        check_shutdown()
        if cancel_check is not None:
            cancel_check()
        return remaining_seconds()

    while True:
        remaining = check_wait()
        if lock.acquire(timeout=0.1 if remaining is None else min(0.1, remaining)):
            break
    try:
        check_wait()
        yield
    finally:
        lock.release()
