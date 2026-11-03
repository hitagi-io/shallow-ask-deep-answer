"""应用日志配置。

所有后端模块都通过 ``get_logger(__name__)`` 获取 logger；只有应用入口负责
配置 handler，避免导入模块时重复初始化日志或在测试中修改全局配置。
"""

from __future__ import annotations

import logging
import os


LOGGER_NAME = "shallow_ask_deep_answer"
_CONFIGURED = False


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
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("pymilvus").setLevel(logging.WARNING)
    _CONFIGURED = True
