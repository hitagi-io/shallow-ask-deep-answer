"""同一 worker 内合并同一回答的读取，慢订阅者只取最新快照。"""
import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import monotonic

import anyio
from starlette.responses import StreamingResponse

POLL_SECONDS = 0.5


class SharedSnapshot:
    def __init__(self, key: tuple[int, str, str]):
        self.key = key
        self.listeners = 0
        self._refresh: asyncio.Task[dict | None] | None = None
        self._refresh_at = 0.0
        self._value: dict | None = None

    async def read(self, fetch: Callable[[], dict | None]) -> dict | None:
        if monotonic() < self._refresh_at:
            return self._value
        if self._refresh is None:
            self._refresh = asyncio.create_task(anyio.to_thread.run_sync(fetch))
            self._refresh.add_done_callback(self._refreshed)
        # 一个订阅到期或断开时只取消它的等待，其他订阅仍可复用在途查询。
        return await asyncio.shield(self._refresh)

    def _refreshed(self, task: asyncio.Task[dict | None]) -> None:
        self._refresh = None
        if not task.cancelled() and task.exception() is None:
            self._value = task.result()
            self._refresh_at = monotonic() + POLL_SECONDS
        self.discard_if_idle()

    def discard_if_idle(self) -> None:
        if not self.listeners and self._refresh is None:
            del _snapshots[self.key]


_snapshots: dict[tuple[int, str, str], SharedSnapshot] = {}


@contextmanager
def watch_generation(key: tuple[int, str, str]) -> Iterator[SharedSnapshot]:
    snapshot = _snapshots.get(key)
    if snapshot is None:
        snapshot = _snapshots[key] = SharedSnapshot(key)
    snapshot.listeners += 1
    try:
        yield snapshot
    finally:
        snapshot.listeners -= 1
        snapshot.discard_if_idle()


class SubscriptionResponse(StreamingResponse):
    def __init__(self, content, *, deadline: float, headers: dict):
        super().__init__(content, media_type="text/event-stream", headers=headers)
        self.deadline = deadline

    async def stream_response(self, send):
        # 即使正文已到期也先发送响应头；下游持续阻塞时，控制帧同样有截止时间。
        try:
            with anyio.fail_after(5):
                await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            with anyio.move_on_after(max(0, self.deadline - monotonic())):
                async for chunk in self.body_iterator:
                    if monotonic() >= self.deadline:
                        break
                    body = chunk.encode(self.charset) if isinstance(chunk, str) else chunk
                    await send({"type": "http.response.body", "body": body, "more_body": True})
            with anyio.fail_after(5):
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except TimeoutError as exc:
            raise OSError("聊天订阅已超时") from exc
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()
