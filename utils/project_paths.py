"""相对项目根目录解析资源路径。"""

import os
def get_project_root():
    current_file=os.path.abspath(__file__)
    current_dir=os.path.dirname(current_file)
    project_root=os.path.dirname(current_dir)
    return project_root
def resolve_project_path(relative_path):
    """将相对路径拼接到项目根目录；传入绝对路径时保留原路径。"""
    return os.path.join(get_project_root(),relative_path)
