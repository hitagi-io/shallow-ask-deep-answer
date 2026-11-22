"""项目资料排序、筛选和页码分页。"""
import os
from datetime import datetime
from typing import Literal, TypedDict

from utils.indexing.upload_tasks import FileUploadOrder


FileFilter = Literal["all", "indexed", "pending"]
FileSort = Literal["uploaded_desc", "uploaded_asc", "name_asc", "name_desc", "pending_first", "indexed_first"]
NAME_SORTS = ("name_asc", "name_desc")


class KnowledgeFile(TypedDict):
    name: str
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


def _sort_key(name: str, upload: FileUploadOrder | None, *, ascending: bool) -> tuple[int, int, int, int, str]:
    if upload is None:
        # 手动放入目录等没有上传记录的资料排最后，不拿修改时间冒充上传时间。
        return 1, 0, 0, 0, name
    elapsed = upload.created_at - datetime.min
    microseconds = (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
    return (0, microseconds if ascending else -microseconds,
            upload.sequence if ascending else -upload.sequence, upload.position, name)


def summarize_files(files: list[KnowledgeFile]) -> FileSummary:
    return {"total": len(files), "indexed": sum(file["indexed"] for file in files)}


def paginate_files(files: list[KnowledgeFile], uploads: list[FileUploadOrder], *,
                   limit: int, page: int, search: str,
                   status: FileFilter, sort: FileSort = "uploaded_desc") -> FilePage:
    search_key = search.strip().casefold()

    candidates = [file for file in files if search_key in file["name"].casefold()
                  and (status == "all" or file["indexed"] == (status == "indexed"))]
    if sort in NAME_SORTS:
        candidates.sort(key=lambda file: (file["name"].casefold(), file["name"]), reverse=sort == "name_desc")
    else:
        orders: dict[str, FileUploadOrder] = {}
        for upload in uploads:
            # Windows 同名大小写按文件系统规则归并，保留已按时间倒序返回的最新一次上传。
            orders.setdefault(os.path.normcase(upload.name), upload)
        candidates.sort(key=lambda file: _sort_key(file["name"], orders.get(os.path.normcase(file["name"])),
                                                   ascending=sort == "uploaded_asc"))
        if sort in ("pending_first", "indexed_first"):
            # 稳定排序保留同一入库状态内的上传时间倒序。
            candidates.sort(key=lambda file: file["indexed"] != (sort == "indexed_first"))
    matched_total = len(candidates)
    total_pages = (matched_total + limit - 1) // limit
    # 末页资料被删除后，回到仍有资料的最后一页；空结果保持第 1 页。
    current_page = min(page, max(1, total_pages))
    offset = (current_page - 1) * limit
    return {"files": candidates[offset:offset + limit],
            "page": current_page, "page_size": limit, "total_pages": total_pages,
            "matched_total": matched_total, "summary": summarize_files(files)}
