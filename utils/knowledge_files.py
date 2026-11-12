"""知识库文件发现、文档加载与内容 SHA-256。"""

import os
import stat
import hashlib
import threading
from collections import OrderedDict
from os import listdir
from langchain_core.documents import Document
from utils.document_parsing import LOADERS
from utils.project_paths import resolve_project_path
from utils.logging_config import get_logger

logger = get_logger(__name__)
_parsed_cache = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_CHARACTERS = 4_000_000


def document_format(file_path: str) -> str:
    """同字节只有使用相同解析器才能共享索引（例如 TXT 与 Markdown 不同）。"""
    return LOADERS[os.path.splitext(file_path)[1].lower()].__name__.removeprefix("load_")


def compute_file_sha256(file_path: str) -> str | None:
    """每次读取实际字节，避免同大小、同 mtime 的覆盖绕过健康检查。"""
    try:
        with open(file_path, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise OSError("不是普通文件")
            digest = hashlib.sha256()
            while chunk := stream.read(65536):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = os.stat(file_path)
        def signature(st):
            return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

        # Windows 的 fstat / stat 对 ctime 的含义可能不同，只在同类调用间比较。
        if (signature(before) != signature(after) or before.st_ctime_ns != after.st_ctime_ns
                or signature(after) != signature(current)):
            raise OSError("计算哈希期间文件发生变化")
        return digest.hexdigest()
    except OSError as exc:
        logger.error("[文件] 计算 SHA-256 失败 file=%s: %s", os.path.basename(file_path), exc)
        return None


def list_supported_files(dir_path,allowed_types:tuple[str]):
    """仅列出项目目录中的普通文件；空项目无需提前创建磁盘目录。"""
    abs_dir_path = resolve_project_path(dir_path)
    files = []
    try:
        names = listdir(abs_dir_path)
    except FileNotFoundError:
        return ()
    for f in names:
        ext = os.path.splitext(f)[1].lower()
        path = os.path.join(abs_dir_path, f)
        if ext in allowed_types and os.path.isfile(path) and not os.path.islink(path):
            files.append(path)
    return tuple(sorted(files))

def load_documents(file_path):
    """缓存有界且按绝对路径和文件摘要隔离，引用回看无需反复执行 OCR。"""
    path = os.path.abspath(file_path)
    digest = compute_file_sha256(path)
    if digest is None:
        raise ValueError("无法读取源文件")
    key = (path, digest)
    with _cache_lock:
        cached = _parsed_cache.get(key)
        if cached is not None:
            _parsed_cache.move_to_end(key)
    if cached is None:
        cached = LOADERS[os.path.splitext(path)[1].lower()](path)
        if compute_file_sha256(path) != digest:
            raise ValueError("解析期间源文件发生变化，请重试")
        size = sum(len(doc.page_content) + len(str(doc.metadata)) for doc in cached)
        if size <= _CACHE_CHARACTERS:
            with _cache_lock:
                # 同路径旧版本立即退出缓存，文件覆盖不会复用旧 OCR 结果。
                for old_key in list(_parsed_cache):
                    if old_key[0] == path:
                        del _parsed_cache[old_key]
                _parsed_cache[key] = cached
                while len(_parsed_cache) > 8 or sum(
                    len(doc.page_content) + len(str(doc.metadata))
                    for docs in _parsed_cache.values() for doc in docs
                ) > _CACHE_CHARACTERS:
                    _parsed_cache.popitem(last=False)
    return [Document(page_content=doc.page_content, metadata=dict(doc.metadata)) for doc in cached]

def get_document_loader(file_path):
    """按扩展名返回文档加载函数；调用该函数后才会读取文件。"""
    ext = os.path.splitext(file_path)[1].lower()
    return load_documents if ext in LOADERS else None
