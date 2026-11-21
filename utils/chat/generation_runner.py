"""独立于 HTTP 订阅的生成生命周期；数据库快照可被任意 worker 读取。"""

import asyncio
import threading
import time

from psycopg import OperationalError

from utils.core.logging_config import get_logger
from utils.chat.stream_bridge import StreamBridge
from utils.chat.checkpoints import CheckpointPublicationError
from utils.chat.outcome import ANSWER_REJECTED_ERROR, AnswerOutcome
from utils.observability.context import execution_deadline, ExecutionStopped
from utils.observability.tracing import span

logger = get_logger(__name__)


async def run_generation(stream_factory, save_progress, finish, *, deadline, on_bridge=lambda bridge: None,
                         save_interval=0.5):
    parts: list[str] = []
    phase = "thinking"
    dirty = False
    outcome: AnswerOutcome | None = None
    ended = False
    error_code = "execution_interrupted"
    status, message_status, error = "failed", "error", "服务暂时不可用，回答已中断，请稍后再试。"
    bridge = None
    try:
        cancel_event = threading.Event()
        with execution_deadline(deadline, cancel_event.is_set):
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
                        status, message_status, error = "cancelled", "cancelled", "已停止回答。"
                        error_code = "cancelled"
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
                elif kind == "outcome":
                    if outcome is not None:
                        raise ValueError("一次生成只能产生一个最终结果")
                    outcome = payload
                elif kind == "end":
                    if outcome is None:
                        raise ValueError("Agent 流缺少最终结果")
                    ended = True
                    error_code = outcome.error_code
                    if outcome.failed:
                        error = "知识库暂时无法查询，请稍后重试。" if outcome.result_kind == "dependency_failed" else ANSWER_REJECTED_ERROR
                    else:
                        status, message_status, error = "completed", "completed", None
                    break
                elif kind == "cancelled":
                    status, message_status, error = "cancelled", "cancelled", "已停止回答。"
                    error_code = "cancelled"
                    break
                else:
                    raise payload
    except TimeoutError:
        error_code = "deadline_exceeded"
        error = "这次回答用时过长，已停止，请稍后再试。"
    except ExecutionStopped:
        status, message_status, error, error_code = "cancelled", "cancelled", "已停止回答。", "cancelled"
    except asyncio.CancelledError:
        error_code = "service_stopping"
    except Exception as exc:
        error_code = type(exc).__name__
        error = "抱歉，这次没能完成回答，请稍后再试。"
        logger.exception("[对话生成] 后台任务失败")
    finally:
        if bridge is not None:
            bridge.stop()
        result_kind = outcome.result_kind if ended and outcome is not None else None
        save_attempt = 1
        while True:
            try:
                with span("generation.save", status=status, error_code=error_code or ""):
                    await asyncio.to_thread(finish, status, message_status, "".join(parts), error,
                                            result_kind=result_kind, error_code=error_code)
                break
            except CheckpointPublicationError:
                if result_kind is None:
                    raise
                logger.exception("[对话生成] 检查点事务已回滚，记录失败结果")
                status, message_status = "failed", "error"
                error, error_code = "会话保存失败，请重新提问。", "checkpoint_save_failed"
                result_kind = None
            except OperationalError:
                logger.exception("[对话生成] 保存终态失败（第 %s 次）", save_attempt)
                if save_attempt == 3:
                    raise
                save_attempt += 1
                await asyncio.sleep(0.5)
    return status
