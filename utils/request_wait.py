"""Cancel a read-only request's waiter when its HTTP client goes away."""
import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from fastapi import HTTPException, Request


T = TypeVar("T")


async def wait_for_client(request: Request, result: Awaitable[T]) -> T:
    async def disconnected():
        # These are GET endpoints with no request body. Waiting on ASGI receive
        # avoids polling and also notices disconnect while the scan is blocked.
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
        # Cache cancellation waits for its last scanner to close all resources.
        await asyncio.gather(work, disconnect, return_exceptions=True)
