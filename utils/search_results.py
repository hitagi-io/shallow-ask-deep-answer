"""保留联网搜索片段与来源的对应关系，限制进入模型上下文的内容量。"""

import json
from urllib.parse import urlsplit

from utils.answer_guard import SEARCH_EMPTY_MARKER, SEARCH_ERROR_MARKER


SEARCH_RESULT_LIMIT = 5
SEARCH_CONTENT_LIMIT = 3000


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _source_url(value: object) -> str:
    url = _text(value)
    if not url or any(char.isspace() for char in url):
        return ""
    try:
        parsed = urlsplit(url)
        return url if parsed.scheme in ("http", "https") and parsed.hostname else ""
    except ValueError:
        return ""


def format_search_response(response: object) -> str:
    """返回来源绑定的 JSON；仅整条失败标记表示异常或无结果。

    summary 是搜索服务给出的综合摘要，不能归于任意一条链接。每个 result 的
    content 才与该 URL 对应，且仅是搜索片段。缺少正文的链接保留为查询线索。
    """
    if not isinstance(response, dict) or response.get("error"):
        return SEARCH_ERROR_MARKER
    if "results" not in response and "answer" not in response:
        return SEARCH_ERROR_MARKER
    raw_results = response.get("results")
    if raw_results is not None and not isinstance(raw_results, list):
        return SEARCH_ERROR_MARKER

    summary = _text(response.get("answer"))
    results = []
    for item in raw_results or []:
        if not isinstance(item, dict):
            continue
        url = _source_url(item.get("url"))
        if not url:
            continue
        content = _text(item.get("content"))
        results.append({
            "title": _text(item.get("title"))[:300] or url,
            "url": url,
            "content": content[:SEARCH_CONTENT_LIMIT],
            "content_truncated": len(content) > SEARCH_CONTENT_LIMIT,
        })
        if len(results) >= SEARCH_RESULT_LIMIT:
            break

    if not summary and not results:
        return SEARCH_EMPTY_MARKER
    return json.dumps({
        "summary": summary[:SEARCH_CONTENT_LIMIT],
        "summary_truncated": len(summary) > SEARCH_CONTENT_LIMIT,
        "results": results,
    }, ensure_ascii=False)
