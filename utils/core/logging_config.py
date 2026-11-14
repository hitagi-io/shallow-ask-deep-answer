"""应用日志配置。

所有后端模块都通过 ``get_logger(__name__)`` 获取 logger；只有应用入口负责
配置 handler，避免导入模块时重复初始化日志或在测试中修改全局配置。
"""

from __future__ import annotations

import logging
import os
import json
from datetime import datetime, timezone
from utils.observability.context import current_context


LOGGER_NAME = "shallow_ask_deep_answer"
_CONFIGURED = False


class ContextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = current_context.get()
        data = {"time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname, "logger": record.name, "message": record.getMessage(),
                "request_id": context.http_request_id, "trace_id": context.trace_id,
                "span_id": context.span_id, "generation_id": context.generation_id, "attempt_id": context.attempt_id}
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def get_logger(module_name: str) -> logging.Logger:
    """返回归属于应用根 logger 的模块 logger。"""
    return logging.getLogger(f"{LOGGER_NAME}.{module_name.rsplit('.', 1)[-1]}")


def configure_logging() -> None:
    """在应用启动阶段配置一次控制台日志。

    ``LOG_LEVEL`` 支持 DEBUG、INFO、WARNING、ERROR、CRITICAL，默认 INFO。
    第三方库的日志固定为 WARNING，避免模型和数据库驱动淹没业务日志。
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    if level_name not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError(f"无效的 LOG_LEVEL: {level_name}")
    handler = logging.StreamHandler()
    handler.setFormatter(ContextFormatter())
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(level_name)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("pymilvus").setLevel(logging.WARNING)
    _CONFIGURED = True
