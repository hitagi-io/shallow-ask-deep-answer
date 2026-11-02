"""知识库文件发现、文档加载与内容 MD5 索引记录管理。"""

import os
import stat
import hashlib
import threading
from os import listdir
from langchain_community.document_loaders import TextLoader, PyPDFLoader, Docx2txtLoader
from utils.project_paths import resolve_project_path
from utils.logging_config import get_logger

logger = get_logger(__name__)

md5_lock = threading.RLock()


_md5_cache: dict[str, tuple[int, int, str]] = {}
_md5_cache_lock = threading.Lock()
_MD5_CACHE_MAX_ENTRIES = 20000


def compute_file_md5(file_path):
    try:
        st = os.stat(file_path)
    except OSError as e:
        logger.error("[文件] 计算 MD5 失败 file=%s: %s", os.path.basename(file_path), e)
        return None
    if not stat.S_ISREG(st.st_mode):
        logger.error("[文件] 计算 MD5 失败 file=%s: 不是普通文件", os.path.basename(file_path))
        return None

    cache_key = os.path.normcase(file_path)
    with _md5_cache_lock:
        cached = _md5_cache.get(cache_key)
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
            return cached[2]

    md5obj = hashlib.md5()
    try:
        with open(file_path, 'rb') as f:
            while chunk := f.read(65536):
                md5obj.update(chunk)
        md5_hex = md5obj.hexdigest()
    except OSError as e:
        logger.error("[文件] 计算 MD5 失败 file=%s: %s", os.path.basename(file_path), e)
        return None

    with _md5_cache_lock:
        if len(_md5_cache) >= _MD5_CACHE_MAX_ENTRIES:
            _md5_cache.clear()
        _md5_cache[cache_key] = (st.st_size, st.st_mtime_ns, md5_hex)
    return md5_hex

def record_indexed_md5(md5_hex):
    """向 md5.txt 追加已入库内容的 MD5 记录。"""
    md5txt_file_path = resolve_project_path(os.path.join("md5.txt"))
    with md5_lock:
        with open(md5txt_file_path, "a", encoding='utf-8') as f:
            f.write(f"{md5_hex}\n")


def has_indexed_md5(md5_hex):
    """检查 MD5 索引记录是否存在；不验证 Milvus 中的向量。"""
    md5txt_file_path = resolve_project_path(os.path.join("md5.txt"))
    with md5_lock:
        if not os.path.exists(md5txt_file_path):
            return False
        with open(md5txt_file_path, "r", encoding='utf-8') as f:
            for line in f:
                if line.strip() == md5_hex:
                    return True
            return False


def read_indexed_md5s() -> set:
    """一次性读取 md5.txt 为集合。

    需要批量判断多个文件是否已入库时使用，避免逐条重新扫描整个记录文件。
    """
    md5txt_file_path = resolve_project_path(os.path.join("md5.txt"))
    with md5_lock:
        if not os.path.exists(md5txt_file_path):
            return set()
        with open(md5txt_file_path, "r", encoding='utf-8') as f:
            return {line.strip() for line in f if line.strip()}

def remove_indexed_md5(md5_hex):
    """移除 MD5 索引记录，不删除磁盘文件或向量。"""
    md5txt_file_path = resolve_project_path(os.path.join("md5.txt"))
    with md5_lock:
        if not os.path.exists(md5txt_file_path):
            return
        with open(md5txt_file_path, "r", encoding='utf-8') as f:
            lines = f.readlines()
        new_lines = [line for line in lines if line.strip() != md5_hex]
        tmp_path = md5txt_file_path + ".tmp"
        with open(tmp_path, "w", encoding='utf-8') as f:
            f.writelines(new_lines)
        os.replace(tmp_path, md5txt_file_path)

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
