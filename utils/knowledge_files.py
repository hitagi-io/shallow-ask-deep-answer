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
    """按扩展名列出当前目录中的路径；目录不存在时创建并返回空结果。"""
    abs_dir_path = resolve_project_path(dir_path)
    files = []
    if not os.path.exists(abs_dir_path):
        os.makedirs(abs_dir_path)
        logger.info("[文件] 目录不存在，已自动创建 directory=%s", os.path.basename(abs_dir_path))
        return files
    if not os.path.isdir(abs_dir_path):
        logger.warning("[文件] 目标路径不是目录 directory=%s", os.path.basename(abs_dir_path))
        return files
    for f in listdir(abs_dir_path):
        ext = os.path.splitext(f)[1].lower()
        if ext in allowed_types:
            files.append(resolve_project_path(os.path.join(dir_path, f)))
    return tuple(files)

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
