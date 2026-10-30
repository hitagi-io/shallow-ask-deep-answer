import os
import yaml
from utils.path_tool import get_abs_path

def load_config(file_path):
    abs_path = get_abs_path(os.path.join('config', file_path))
    try:
        with open(abs_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError as e:
        # 配置缺失会让后续取值直接抛 TypeError，这里提前用明确的信息终止启动
        raise RuntimeError(f"配置文件不存在: {abs_path}") from e
    except Exception as e:
        raise RuntimeError(f"加载配置失败 ({abs_path}): {e}") from e

agent_config=load_config('agent.yaml')
vector_db_config=load_config('vector_db.yaml')
