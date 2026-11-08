"""Run blocking read-only probes with a hard deadline and reap them before returning.

Cancelling a thread does not stop a blocked filesystem/libpq/gRPC call. A separate
process owns these resources so timeout/cancellation can actually close them.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import json
import os
from pathlib import Path
import subprocess
import sys
import time


async def run_isolated_json(module: str, payload: dict, timeout_seconds: float) -> dict:
    """Exchange one JSON request/result with a Python module, including startup in the budget."""
    if timeout_seconds <= 0:
        raise TimeoutError("检查执行超时")
    process = None
    startup = None
    communicate = None
    expires_at = time.monotonic() + timeout_seconds
    # A saturated business executor must not delay startup or cleanup beyond the
    # deadline. Admission limits live in the caller; each admitted probe owns one
    # short-lived communication thread, which is joined before this call returns.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="health-process")
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(timeout_seconds):
            # Uvicorn's Windows multi-worker SelectorEventLoop cannot use asyncio
            # subprocess transports. Only the admitted scan uses a communication
            # thread; killing and reaping its process also joins that thread.
            startup = loop.run_in_executor(executor, partial(subprocess.Popen,
                # 继承 -s / -I / -X 等参数，防止检查进程加载另一套解析依赖。
                [sys.executable, *subprocess._args_from_interpreter_flags(), "-m", module],
                cwd=str(Path(__file__).resolve().parent.parent),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Leave diagnostics on the server's stderr; stdout is the JSON protocol.
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            ))
            # If cancellation lands during spawn, retain the handle and reap it below.
            process = await asyncio.shield(startup)
            request = {**payload, "timeout_seconds": max(0.001, expires_at - time.monotonic())}
            communicate = loop.run_in_executor(executor,
                process.communicate, json.dumps(request).encode("utf-8"))
            stdout, _ = await asyncio.shield(communicate)
            if process.returncode != 0:
                raise RuntimeError("检查进程异常退出，详情见服务端日志")
            result = json.loads(stdout)
            if not isinstance(result, dict):
                raise RuntimeError("检查进程返回格式错误")
            return result
    finally:
        # Never free an admission slot or let a replacement scan start while its
        # predecessor still owns sockets/locks. Shield cleanup from caller cancellation.
        async def cleanup():
            nonlocal process
            if process is None and startup is not None:
                try:
                    process = await startup
                except Exception:
                    return
            if process is not None:
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                if communicate is None:
                    await loop.run_in_executor(executor, process.wait)
                    if process.stdin is not None:
                        process.stdin.close()
                    if process.stdout is not None:
                        process.stdout.close()
            if communicate is not None:
                await asyncio.gather(communicate, return_exceptions=True)

        cleanup_task = asyncio.create_task(cleanup())
        cancelled = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # Cancellation already propagates from the protected operation;
                # repeated cancellation must still not orphan the child.
                cancelled = True
        try:
            cleanup_task.result()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        if cancelled:
            raise asyncio.CancelledError
