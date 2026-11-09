"""把同步生成器桥接到 asyncio 事件循环，并支持跨线程取消。

Agent 的流式接口是同步生成器，只能在独立线程里拉取。直接把 `next()` 放进
`anyio.to_thread.run_sync` 的问题在于：取消只会在该次 `next()` 返回后生效，
而生成器又不能在正在执行的线程之外 `close()`（会抛 ValueError）。

本模块让生产线程独占生成器：
- 结果通过 `loop.call_soon_threadsafe` 写入 asyncio 队列，事件循环侧可以随时
  用 `asyncio.wait_for` / `asyncio.timeout` 结束等待，不必等线程返回；
- `stop()` 只设置线程安全的事件，生产线程在下一个 `next()` 返回后退出，
  并在自己的线程里 `close()` 生成器，从而把 GeneratorExit 传给上游模型流。

事件类型：("chunk", text) / ("replace", text) / ("phase", phase) / ("end", None)
/ ("cancelled", None) / ("error", exc)。

上游生成器可产出 str（普通文本增量）或 dict：
- {"type": "replace", "content": text}：整段替换已下发内容；
- {"type": "phase", "phase": name}：阶段提示。
"""

from __future__ import annotations

import asyncio
import threading

from utils.logging_config import get_logger

logger = get_logger(__name__)


class StreamBridge:
    """同步生成器 → asyncio 队列；`stop()` 后以 cancelled 事件结束。"""

    THREAD_NAME = "chat-upstream"

    def __init__(self, sync_stream, cancel_event: threading.Event | None = None):
        self._stream = sync_stream
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._stopped = threading.Event()
        # 共享给内层包装器（如 kb 模式的缓冲循环），让取消能穿透到原始消息流
        self._cancel_event = cancel_event
        self._thread = threading.Thread(
            target=self._run, name=self.THREAD_NAME, daemon=True
        )
        self._thread.start()

    def _emit(self, kind: str, payload=None) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (kind, payload))
        except RuntimeError:
            # 事件循环已关闭（服务退出），丢弃结果即可
            pass

    def _run(self) -> None:
        try:
            for item in self._stream:
                if self._stopped.is_set():
                    self._emit("cancelled")
                    return
                if isinstance(item, dict):
                    event_type = item["type"]
                    if event_type == "phase":
                        self._emit("phase", item["phase"])
                        continue
                    if event_type == "replace":
                        self._emit("replace", item["content"])
                        continue
                    raise ValueError(f"未知的对话流事件: {event_type}")
                self._emit("chunk", item)
            if self._stopped.is_set():
                self._emit("cancelled")
            else:
                self._emit("end")
        except Exception as e:
            self._emit("error", e)
        finally:
            # 必须在本线程内关闭：生成器不能跨线程 close()
            try:
                self._stream.close()
            except Exception as e:
                logger.error(f"[对话流] 关闭上游生成器失败，继续结束流式任务: {e}")

    def stop(self) -> None:
        """请求取消；生产线程会在下一次 next() 返回或抛错时退出。"""
        self._stopped.set()
        if self._cancel_event is not None:
            self._cancel_event.set()

    async def receive(self):
        """等待下一个事件；调用方取消等待不会影响生产线程，需配合 stop()。"""
        return await self._queue.get()
