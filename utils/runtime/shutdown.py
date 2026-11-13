"""服务关闭时，在可回滚的工作边界通知后台线程停止。"""

import threading
from contextlib import contextmanager

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
def interruptible_lock(lock):
    """等待其他 OCR/解析任务时也能响应关闭，持锁工作由调用方设置检查点。"""
    check_shutdown()
    while not lock.acquire(timeout=0.1):
        check_shutdown()
    try:
        check_shutdown()
        yield
    finally:
        lock.release()
