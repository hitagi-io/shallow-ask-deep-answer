"""本机 Any2Markdown 的官方 HTTP/SSE 接口。按原页序交付完整的转换结果。"""

import ipaddress
import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from utils.observability.context import remaining_seconds
from utils.runtime.shutdown import check_shutdown


@dataclass(frozen=True)
class ConvertedPage:
    index: int
    markdown: str
    ocr: bool


def _local_url(value: str, *, model=False) -> str:
    url = urlsplit(value)
    host = url.hostname or ""
    local = host == "localhost" or (model and host == "xiaomi-ocr")
    if not local:
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if (not local or url.scheme not in ("http", "https") or url.username is not None
            or url.password is not None or url.query or url.fragment):
        raise ValueError("Any2Markdown 和 Xiaomi OCR 必须使用本机服务地址")
    return value.rstrip("/")


def _time_left(deadline: float) -> float:
    check_shutdown()
    remaining = remaining_seconds()
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("Any2Markdown 文档解析超过时限")
    return min(left, remaining) if remaining is not None else left


@contextmanager
def _connection():
    seconds = float(os.getenv("ANY2MARKDOWN_TIMEOUT_SECONDS", "1800"))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("ANY2MARKDOWN_TIMEOUT_SECONDS 必须是有限正数")
    url = _local_url(os.getenv("ANY2MARKDOWN_URL", "http://127.0.0.1:8088"))
    deadline = time.monotonic() + seconds
    seconds = _time_left(deadline)
    try:
        with httpx.Client(base_url=url, trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(seconds, connect=min(seconds, 5))) as client:
            yield client, deadline
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"Any2Markdown 接口返回 HTTP {exc.response.status_code}") from exc
    except httpx.TimeoutException as exc:
        raise TimeoutError("等待本机 Any2Markdown 服务超时") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("无法连接本机 Any2Markdown，请检查解析服务和小米模型服务") from exc


def _check_model(client, deadline):
    # 不指定可变的网页 profile。服务使用启动时的 converter，与 /health 来自同一配置。
    response = client.get("health", timeout=min(_time_left(deadline), 5))
    response.raise_for_status()
    settings = response.json()
    if settings.get("model") != "xiaomi-ocr-0" or settings.get("table_format") != "html":
        raise ValueError("请将 Any2Markdown 服务设置为模型 xiaomi-ocr-0 和 HTML 表格输出")
    _local_url(settings.get("base_url") or "", model=True)


def _events(response, deadline):
    # httpx.iter_lines() 也将 U+2028 等文档字符拆行。SSE 帧使用 ASCII 换行符。
    buffer = bytearray()
    kind = b""
    for chunk in response.iter_bytes():
        _time_left(deadline)
        parts = chunk.split(b"\n")
        for part in parts[:-1]:
            buffer.extend(part)
            line = bytes(buffer).removesuffix(b"\r")
            buffer.clear()
            if line.startswith(b"event:"):
                kind = line[6:].strip()
            elif line.startswith(b"data:"):
                payload = json.loads(line[5:])
                if not isinstance(payload, dict):
                    raise ValueError("Any2Markdown 返回了无效的页面事件")
                yield kind, payload
                kind = b""
        buffer.extend(parts[-1])


def _pages(response, deadline, mode, on_progress):
    pages = {}
    total = None
    ocr_indices = set()
    for kind, data in _events(response, deadline):
        if kind == b"error":
            detail = str(data.get("error") or "")
            if (mode == "direct" and data.get("status") == 422
                    and detail.endswith("use parse_mode=auto or ocr")
                    and ("has no usable text layer" in detail or "cannot be fully parsed directly" in detail)):
                return []
            raise ValueError(f"Any2Markdown 解析失败，请检查服务日志（状态 {data.get('status')}）")
        if kind == b"meta":
            count, indices = data.get("total_pages"), data.get("ocr_pages")
            if (total is not None or type(count) is not int or count < 0 or not isinstance(indices, list)
                    or any(type(i) is not int or not 0 <= i < count for i in indices)):
                raise ValueError("Any2Markdown 返回了无效的页序")
            total, ocr_indices = count, set(indices)
            if on_progress is not None and total:
                on_progress(0, total)
        elif kind == b"page":
            index = data.get("page_index")
            if total is None or type(index) is not int or not 0 <= index < total or index in pages:
                raise ValueError("Any2Markdown 返回了重复或无效的页序")
            if data.get("success") is not True or not isinstance(data.get("markdown"), str):
                raise ValueError(f"Any2Markdown 第 {index + 1} 页解析失败，请检查解析和模型服务")
            pages[index] = ConvertedPage(index, data["markdown"], index in ocr_indices)
            if on_progress is not None:
                on_progress(len(pages), total)
        elif kind == b"done":
            if (total is None or total != data.get("total_pages") or len(pages) != total
                    or data.get("failed_pages") != []
                    or (mode == "ocr" and len(ocr_indices) != total) or (mode == "direct" and ocr_indices)):
                raise ValueError("Any2Markdown 未返回完整的逐页解析结果")
            return [pages[i] for i in range(total)]
    raise ValueError("Any2Markdown 页面流提前结束")


def _convert_upload(name, stream, mode, on_progress=None):
    with _connection() as (client, deadline):
        if mode != "direct":
            _check_model(client, deadline)
        left = _time_left(deadline)
        with client.stream("POST", "convert/file/stream", files={"file": (name, stream)},
                           data={"parse_mode": mode}, timeout=httpx.Timeout(left, connect=min(left, 5))) as response:
            response.raise_for_status()
            return _pages(response, deadline, mode, on_progress)


def convert_file(path, *, mode="auto", on_progress=None) -> list[ConvertedPage]:
    path = Path(path)
    with path.open("rb") as stream:
        return _convert_upload(path.name, stream, mode, on_progress)


def convert_png(stream) -> str:
    pages = _convert_upload("image.png", stream, "ocr")
    if len(pages) != 1:
        raise ValueError("Any2Markdown 单张图片应返回一页结果")
    return pages[0].markdown


def convert_text(text: str, mime: str) -> str:
    with _connection() as (client, deadline):
        response = client.post("convert/text", json={"text": text, "mime": mime})
        _time_left(deadline)
        response.raise_for_status()
        markdown = response.json().get("markdown")
        if not isinstance(markdown, str):
            raise ValueError("Any2Markdown 未返回文本转换结果")
        return markdown
