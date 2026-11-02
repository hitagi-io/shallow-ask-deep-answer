"""独立于 HTTP 订阅的生成生命周期；数据库快照可被任意 worker 读取。"""

import asyncio
import threading
import time

from utils.logging_config import get_logger
from utils.stream_bridge import StreamBridge

logger = get_logger(__name__)


async def run_generation(stream_factory, save_progress, finish, *, deadline, on_bridge=lambda bridge: None,
                         save_interval=0.5):
    parts: list[str] = []
    phase = "thinking"
    dirty = False
    status, message_status, error = "failed", "error", "生成中断：服务正在关闭"
    bridge = None
    try:
        cancel_event = threading.Event()
        bridge = StreamBridge(stream_factory(cancel_event.is_set), cancel_event=cancel_event)
        on_bridge(bridge)
        next_save = time.monotonic() + save_interval
        async with asyncio.timeout(deadline):
            while True:
                if time.monotonic() >= next_save:
                    running = await asyncio.to_thread(save_progress, "".join(parts) if dirty else None, phase)
                    dirty = False
                    next_save = time.monotonic() + save_interval
                    if not running:
                        status, message_status, error = "cancelled", "cancelled", "回答已取消"
                        break
                try:
                    kind, payload = await asyncio.wait_for(bridge.receive(), max(0.001, next_save - time.monotonic()))
                except TimeoutError:
                    continue
                if kind == "chunk":
                    parts.append(payload)
                    dirty = True
                elif kind == "replace":
                    parts[:] = [payload]
                    dirty = True
                elif kind == "phase":
                    phase = payload
                elif kind == "end":
                    status, message_status, error = "completed", "completed", None
                    break
                elif kind == "cancelled":
                    status, message_status, error = "cancelled", "cancelled", "回答已取消"
                    break
                else:
                    raise payload
    except TimeoutError:
        error = "回答超时，请稍后重试"
    except asyncio.CancelledError:
        pass  # 服务退出时保留已生成内容；HTTP 断开不会取消此任务。
    except Exception:
        error = "服务处理异常，请稍后重试"
        logger.exception("[对话生成] 后台任务失败")
    finally:
        if bridge is not None:
            bridge.stop()
        for attempt in range(3):
            try:
                await asyncio.to_thread(finish, status, message_status, "".join(parts), error)
                break
            except Exception:
                logger.exception("[对话生成] 保存终态失败（第 %s 次）", attempt + 1)
                if attempt < 2:
                    await asyncio.sleep(0.5)
