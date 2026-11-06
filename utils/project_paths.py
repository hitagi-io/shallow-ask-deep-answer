"""源码资源路径与账号、项目隔离的外部知识库路径。"""

import os
import uuid
from pathlib import Path

def get_project_root():
    current_file=os.path.abspath(__file__)
    current_dir=os.path.dirname(current_file)
    project_root=os.path.dirname(current_dir)
    return project_root
def resolve_project_path(relative_path):
    """将相对路径拼接到项目根目录；传入绝对路径时保留原路径。"""
    return os.path.join(get_project_root(),relative_path)


def knowledge_scope(user_id: int, project_id: str) -> tuple[int, str]:
    """只使用服务端账号 ID 和规范 UUID 构造磁盘路径、集合名。"""
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("无效的账号 ID")
    return user_id, str(uuid.UUID(str(project_id)))


def get_knowledge_storage_root() -> str:
    source_root = Path(get_project_root()).resolve()
    configured = os.getenv("KNOWLEDGE_STORAGE_ROOT")
    root = Path(configured).expanduser() if configured else source_root.parent / f"{source_root.name}-data"
    if not root.is_absolute():
        raise ValueError("KNOWLEDGE_STORAGE_ROOT 必须是项目外的绝对路径")
    root = root.resolve()
    if root == source_root or source_root in root.parents:
        raise ValueError("知识库文件目录必须位于源码项目之外")
    return str(root)


def get_knowledge_dir(user_id: int, project_id: str) -> str:
    user_id, project_id = knowledge_scope(user_id, project_id)
    root = Path(get_knowledge_storage_root())
    directory = root / str(user_id) / project_id
    # 不允许现有符号链接将一个项目重定向到另一个项目或源码目录。
    if directory.resolve() != directory:
        raise ValueError("知识库目录不能通过链接重定向")
    return str(directory)


def scoped_knowledge_file(directory: str, file_path: str) -> str:
    """校验文件确实位于当前项目的直接目录中，再允许读取或变更。"""
    root = Path(directory).resolve()
    path = Path(file_path)
    if not path.is_absolute():
        path = root / path
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("文件不属于当前知识库项目")
    return str(path.resolve())
