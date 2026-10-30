import os
from string import Template

from utils.answer_guard import (
    COPY_KB_DOWN,
    COPY_KB_MISS,
    RETRIEVAL_ERROR_MARKER,
    SEARCH_EMPTY_MARKER,
    SEARCH_ERROR_MARKER,
    SENTINEL_KB_DOWN,
    SENTINEL_KB_MISS,
)
from utils.path_tool import get_abs_path

def load_prompts(prompt_name):
    abs_path = get_abs_path(os.path.join('prompts_data', prompt_name))
    try:
        with open(abs_path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError as e:
        # 提示词缺失无法降级运行，直接以明确的信息终止启动
        raise RuntimeError(f"提示词文件不存在: {abs_path}") from e
    except Exception as e:
        raise RuntimeError(f"加载提示词失败 ({abs_path}): {e}") from e

def _render(prompt_name, **values):
    """渲染提示词模板（$name / ${name} 占位符），缺失变量直接报错，避免带占位符上线。"""
    raw = load_prompts(prompt_name)
    return Template(raw).substitute(**values)

def render_main_prompt():
    """渲染「仅知识库」模式（默认）主提示词。

    固定文案与固定短句统一取自 utils.answer_guard，避免两处不一致。
    """
    return _render(
        'main_prompt.txt',
        sentinel_kb_down=SENTINEL_KB_DOWN,
        sentinel_kb_miss=SENTINEL_KB_MISS,
        copy_kb_down=COPY_KB_DOWN,
        copy_kb_miss=COPY_KB_MISS,
    )

def render_summary_prompt():
    """渲染检索子代理提示词：固定短句与故障标记同样取自 utils.answer_guard。"""
    return _render(
        'summary_prompt.txt',
        sentinel_kb_down=SENTINEL_KB_DOWN,
        sentinel_kb_miss=SENTINEL_KB_MISS,
        retrieval_error_marker=RETRIEVAL_ERROR_MARKER,
    )

def render_general_prompt():
    """渲染通用模式（关闭「仅知识库」）主提示词：仅注入联网搜索的两个异常标记。"""
    return _render(
        'general_prompt.txt',
        search_error_marker=SEARCH_ERROR_MARKER,
        search_empty_marker=SEARCH_EMPTY_MARKER,
    )