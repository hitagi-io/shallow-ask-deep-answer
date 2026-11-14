"""DashScope 原生 HTTP 请求：显式超时、有限重试，不记录正文和密钥。"""
from __future__ import annotations

import math
import os
import time
from urllib.parse import urlsplit

import httpx

from utils.core.logging_config import get_logger
from utils.runtime.shutdown import check_shutdown
from utils.observability.context import remaining_seconds, ExecutionDeadlineExceeded
from utils.observability.tracing import span

logger = get_logger(__name__)
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class DashScopeRequestError(RuntimeError):
    """上游模型请求失败；由知识库工具统一转换为服务故障。"""


def validate_request_settings(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("模型请求配置必须为键值映射")
    endpoint = config.get("endpoint")
    if not isinstance(endpoint, str):
        raise ValueError("模型 endpoint 必须为 HTTPS 地址")
    url = urlsplit(endpoint)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("模型 endpoint 必须为不含凭证的 HTTPS 地址")
    for name in ("timeout_seconds", "connect_timeout_seconds"):
        value = config.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} 必须为有限正数")
    attempts = config.get("max_attempts")
    if type(attempts) is not int or not 1 <= attempts <= 5:
        raise ValueError("max_attempts 必须为 1～5 的整数（包含首次请求）")
    return config


def make_client(config: dict) -> httpx.Client:
    """使用嵌入适配器或检索模块初始化时已校验的请求配置。"""
    return httpx.Client(
        timeout=httpx.Timeout(config["timeout_seconds"], connect=config["connect_timeout_seconds"]),
        trust_env=False,
    )


def request_json(client: httpx.Client, config: dict, payload: dict, operation: str) -> dict:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise ValueError("缺少 DASHSCOPE_API_KEY")
    attempts = config["max_attempts"]
    for attempt in range(1, attempts + 1):
        check_shutdown()
        remaining = remaining_seconds()
        request_timeout = config["timeout_seconds"] if remaining is None else min(config["timeout_seconds"], remaining)
        try:
            with span("provider.request", operation=operation, attempt=attempt) as request_span:
                response = client.post(config["endpoint"], json=payload,
                                       headers={"Authorization": "Bearer " + api_key},
                                       timeout=httpx.Timeout(request_timeout, connect=min(config["connect_timeout_seconds"], request_timeout)))
                request_span.attributes["http_status"] = response.status_code
                if not response.is_success:
                    request_span.fail(f"http_{response.status_code}")
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            reason = type(exc).__name__
            if attempt == attempts:
                raise DashScopeRequestError(f"{operation} 连接失败（{reason}），已尝试 {attempt} 次") from exc
        else:
            if response.is_success:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise DashScopeRequestError(f"{operation} 响应不是有效 JSON") from exc
                if not isinstance(body, dict):
                    raise DashScopeRequestError(f"{operation} 响应不是 JSON 对象")
                return body
            reason = f"HTTP {response.status_code}"
            if response.status_code not in _RETRYABLE_STATUS or attempt == attempts:
                raise DashScopeRequestError(f"{operation} 返回 {reason}，已尝试 {attempt} 次")
        check_shutdown()
        logger.warning("[模型请求] %s 第 %s/%s 次失败（%s），稍后重试", operation, attempt, attempts, reason)
        delay = min(2 ** (attempt - 1), 8)
        remaining = remaining_seconds()
        if remaining is not None and delay >= remaining:
            raise ExecutionDeadlineExceeded("剩余生成时间不足以重试")
        # 退避期间也响应停止；不在上层重新执行整个 Agent 或上传任务。
        until = time.monotonic() + delay
        while time.monotonic() < until:
            check_shutdown()
            remaining_seconds()
            time.sleep(min(0.1, max(0, until - time.monotonic())))
    raise DashScopeRequestError(f"{operation} 未返回结果")
