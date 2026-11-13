"""以硬性截止时间执行阻塞式只读探测，并在返回前回收探测进程。

取消线程无法停止已阻塞的文件系统、libpq 或 gRPC 调用。
由独立进程持有这些资源，才能在超时或取消时真正关闭它们。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import json
import os
import subprocess
import sys
import time

from utils.core.project_paths import get_project_root


async def run_isolated_json(module: str, payload: dict, timeout_seconds: float) -> dict:
    """与独立运行的 Python 模块交换一组 JSON 请求和结果；启动耗时计入总时限。"""
    if timeout_seconds <= 0:
        raise TimeoutError("检查执行超时")
    process = None
    startup = None
    communicate = None
    expires_at = time.monotonic() + timeout_seconds
    # 业务线程池满载时，不能导致探测启动或清理超时。
    # 调用方限制并发；每个获准执行的探测使用一个短期通信线程，
    # 并在本次调用返回前等待该线程退出。
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="health-process")
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(timeout_seconds):
            # Uvicorn 在 Windows 多工作进程模式下使用的 SelectorEventLoop
            # 不支持 asyncio 子进程传输。仅为获准执行的扫描创建通信线程，
            # 终止并回收扫描进程时也等待该线程退出。
            startup = loop.run_in_executor(executor, partial(subprocess.Popen,
                # 继承 -s / -I / -X 等参数，防止检查进程加载另一套解析依赖。
                [sys.executable, *subprocess._args_from_interpreter_flags(), "-m", module],
                cwd=get_project_root(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # 诊断信息沿用服务端标准错误输出；标准输出仅承载 JSON 协议。
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            ))
            # 创建进程期间发生取消时，仍保留进程句柄，供下方清理逻辑回收。
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
        # 上一轮扫描仍持有套接字或锁时，不释放并发名额，也不启动替代扫描。
        # 清理过程不受调用方取消影响。
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
                # 原操作的取消会继续向上传播；即使再次收到取消，
                # 也必须回收子进程，不能使其成为孤儿进程。
                cancelled = True
        try:
            cleanup_task.result()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        if cancelled:
            raise asyncio.CancelledError
