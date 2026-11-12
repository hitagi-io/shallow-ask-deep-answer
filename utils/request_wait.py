"""HTTP 客户端断开时，取消对应只读请求的等待任务。"""
import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from fastapi import HTTPException, Request


T = TypeVar("T")


async def wait_for_client(request: Request, result: Awaitable[T]) -> T:
    async def disconnected():
        # 这些 GET 接口没有请求体；等待 ASGI 接收消息可避免轮询，
        # 并能在扫描阻塞期间发现客户端断开。
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return

    work = asyncio.ensure_future(result)
    disconnect = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((work, disconnect), return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()
        raise HTTPException(status_code=499, detail="请求已取消")
    finally:
        for task in (work, disconnect):
            if not task.done():
                task.cancel()
        # 取消缓存等待时，会先等待最后一个扫描任务关闭所有资源。
        await asyncio.gather(work, disconnect, return_exceptions=True)
