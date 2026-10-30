import os
from string import Template

from utils.answer_guard import (
    COPY_KB_DOWN,
    COPY_KB_MISS,
    COPY_KB_UNVERIFIED,
    SEARCH_EMPTY_MARKER,
    SEARCH_ERROR_MARKER,
)
from utils.retrieval_protocol import (
    DIRECT_CATEGORIES,
    REPORT_PROTOCOL,
    RETRIEVAL_EMPTY_MARKER,
    RETRIEVAL_ERROR_MARKER,
    SENTINEL_KB_DOWN,
    SENTINEL_KB_MISS,
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_INVALID,
    STATUS_NO_RETRIEVAL,
    STATUS_OK,
)
from utils.path_tool import get_abs_path

def load_prompts(prompt_name):
    abs_path = get_abs_path(os.path.join('prompts_data', prompt_name))
    try:
        with open(abs_path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError as e:
        raise RuntimeError(f"提示词文件不存在: {abs_path}") from e
    except Exception as e:
        raise RuntimeError(f"加载提示词失败 ({abs_path}): {e}") from e

def _render(prompt_name, **values):
    """渲染提示词模板（$name / ${name} 占位符），缺失变量直接报错，避免带占位符上线。"""
    raw = load_prompts(prompt_name)
    return Template(raw).substitute(**values)

def render_main_prompt():
    """渲染「仅知识库」模式（默认）主提示词。

    固定文案、结构化协议字段与直答类别统一取自代码常量，避免提示词与判定逻辑漂移。
    """
    return _render(
        'main_prompt.txt',
        report_protocol=REPORT_PROTOCOL,
        status_ok=STATUS_OK,
        status_empty=STATUS_EMPTY,
        status_error=STATUS_ERROR,
        status_no_retrieval=STATUS_NO_RETRIEVAL,
        status_invalid=STATUS_INVALID,
        copy_kb_down=COPY_KB_DOWN,
        copy_kb_miss=COPY_KB_MISS,
        copy_kb_unverified=COPY_KB_UNVERIFIED,
        direct_categories="、".join(DIRECT_CATEGORIES),
    )

def render_summary_prompt():
    """渲染检索子代理提示词：标记与固定短句同样取自代码常量。"""
    return _render(
        'summary_prompt.txt',
        sentinel_kb_down=SENTINEL_KB_DOWN,
        sentinel_kb_miss=SENTINEL_KB_MISS,
        retrieval_error_marker=RETRIEVAL_ERROR_MARKER,
        retrieval_empty_marker=RETRIEVAL_EMPTY_MARKER,
    )

def render_general_prompt():
    """渲染通用模式（关闭「仅知识库」）主提示词：仅注入联网搜索的两个异常标记。"""
    return _render(
        'general_prompt.txt',
        search_error_marker=SEARCH_ERROR_MARKER,
        search_empty_marker=SEARCH_EMPTY_MARKER,
    )
