import os
import yaml
from utils.project_paths import resolve_project_path

def load_config(file_path):
    abs_path = resolve_project_path(os.path.join('config', file_path))
    try:
        with open(abs_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError as e:
        raise RuntimeError(f"配置文件不存在: {abs_path}") from e
    except Exception as e:
        raise RuntimeError(f"加载配置失败 ({abs_path}): {e}") from e

def get_kb_subagent_max_retrieval_calls() -> int:
    """统一读取检索上限，拒绝布尔值、非整数及非正数配置。"""
    return _positive_int(agent_config, "kb_subagent_max_retrieval_calls")


def _positive_int(config: dict, key: str) -> int:
    value = config.get(key)
    if type(value) is not int or value <= 0:
        raise ValueError(f"config/agent.yaml 的 {key} 必须为正整数")
    return value


def validate_agent_config(config: dict) -> None:
    """在启动时检查检索与摘要参数，避免无效配置进入工具或中间件。"""
    if not isinstance(config, dict):
        raise ValueError("config/agent.yaml 必须为键值映射")
    for key in (
        "kb_subagent_max_retrieval_calls",
        "search_result_limit",
        "search_content_limit",
        "summary_trigger_messages",
        "summary_trigger_tokens",
        "summary_keep_messages",
    ):
        _positive_int(config, key)
    # Tavily Search API 的 max_results 上限为 20；本项目要求至少一条来源。
    if config["search_result_limit"] > 20:
        raise ValueError("config/agent.yaml 的 search_result_limit 必须在 1～20 之间")
    if config["summary_keep_messages"] >= config["summary_trigger_messages"]:
        raise ValueError(
            "config/agent.yaml 的 summary_keep_messages 必须小于 summary_trigger_messages"
        )


agent_config=load_config('agent.yaml')
validate_agent_config(agent_config)
vector_db_config=load_config('vector_db.yaml')
