"""项目资料排序、筛选和页码分页。"""
import os
from datetime import datetime
from typing import Literal, TypedDict

from utils.indexing.upload_tasks import FileUploadOrder


FileFilter = Literal["all", "indexed", "pending"]
FileSort = Literal["uploaded_desc", "uploaded_asc", "name_asc", "name_desc", "pending_first", "indexed_first"]


class KnowledgeFile(TypedDict):
    name: str
    path: str
    indexed: bool


class FileSummary(TypedDict):
    total: int
    indexed: int


class FilePage(TypedDict):
    files: list[KnowledgeFile]
    page: int
    page_size: int
    total_pages: int
    matched_total: int
    summary: FileSummary


def _sort_key(name: str, created_at: datetime | None, sequence: int | None,
              position: int | None, *, ascending: bool) -> tuple[int, int, int, int, str]:
    if created_at is None:
        # 手动放入目录等没有上传记录的资料排最后，不拿修改时间冒充上传时间。
        return 1, 0, 0, 0, name
    if sequence is None or position is None:
        raise ValueError("上传顺序信息不完整")
    elapsed = created_at - datetime.min
    microseconds = (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
    return 0, microseconds if ascending else -microseconds, sequence if ascending else -sequence, position, name


def summarize_files(files: list[KnowledgeFile]) -> FileSummary:
    return {"total": len(files), "indexed": sum(file["indexed"] for file in files)}


def paginate_files(files: list[KnowledgeFile], uploads: list[FileUploadOrder], *,
                   limit: int, page: int, search: str,
                   status: FileFilter, sort: FileSort = "uploaded_desc") -> FilePage:
    search_key = search.strip().casefold()

    orders: dict[str, FileUploadOrder] = {}
    for upload in uploads:
        # Windows 同名大小写按文件系统规则归并，保留已按时间倒序返回的最新一次上传。
        orders.setdefault(os.path.normcase(upload.name), upload)

    candidates = []
    for file in files:
        if search_key not in file["name"].casefold() or (status != "all" and file["indexed"] != (status == "indexed")):
            continue
        upload = orders.get(os.path.normcase(file["name"]))
        key = _sort_key(file["name"], upload.created_at if upload else None,
                        upload.sequence if upload else None, upload.position if upload else None,
                        ascending=sort == "uploaded_asc")
        candidates.append((key, file))
    candidates.sort(key=lambda entry: entry[0])
    if sort in ("name_asc", "name_desc"):
        candidates.sort(key=lambda entry: (entry[1]["name"].casefold(), entry[1]["name"]), reverse=sort == "name_desc")
    elif sort in ("pending_first", "indexed_first"):
        # 稳定排序保留同一入库状态内的上传时间倒序。
        candidates.sort(key=lambda entry: entry[1]["indexed"] != (sort == "indexed_first"))
    matched_total = len(candidates)
    total_pages = (matched_total + limit - 1) // limit
    # 末页资料被删除后，回到仍有资料的最后一页；空结果保持第 1 页。
    current_page = min(page, max(1, total_pages))
    offset = (current_page - 1) * limit
    return {"files": [entry[1] for entry in candidates[offset:offset + limit]],
            "page": current_page, "page_size": limit, "total_pages": total_pages,
            "matched_total": matched_total, "summary": summarize_files(files)}
