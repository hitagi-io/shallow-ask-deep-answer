"""知识库文件发现、文档加载与内容 SHA-256。"""

import os
import stat
import hashlib
from os import listdir
from langchain_community.document_loaders import TextLoader, PyPDFLoader, Docx2txtLoader
from utils.project_paths import resolve_project_path
from utils.logging_config import get_logger

logger = get_logger(__name__)


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

def load_text_documents(file_path):
    return TextLoader(file_path, encoding='utf-8').load()

def load_pdf_documents(file_path):
    return PyPDFLoader(file_path).load()

def load_docx_documents(file_path):
    return Docx2txtLoader(file_path).load()

def get_document_loader(file_path):
    """按扩展名返回文档加载函数；调用该函数后才会读取文件。"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".txt":
        return load_text_documents
    elif ext == ".pdf":
        return load_pdf_documents
    elif ext == ".docx":
        return load_docx_documents
    else:
        return
