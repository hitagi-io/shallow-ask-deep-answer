"""项目鉴权完成后，从 ASGI 请求流有界地接收 multipart 文件。"""
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import anyio
from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser


class _UploadParser(MultiPartParser):
    def __init__(self, request: Request, stream: AsyncIterator[bytes], file_limit: int,
                 check_bytes: Callable[[int], None]):
        super().__init__(request.headers, stream, max_files=32, max_fields=32)
        self.file_limit = file_limit
        self.check_bytes = check_bytes
        self.spooled_bytes = 0
        self.oversized: set[UploadFile] = set()
        self.complete = False

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self.part_bytes = 0
        self.header_bytes = 0

    def _count_header(self, size: int) -> None:
        self.header_bytes += size
        if self.header_bytes > 16 * 1024:
            raise MultiPartException("上传表单的文件头过长")

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._count_header(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._count_header(end - start)
        super().on_header_value(data, start, end)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        file = self._current_part.file
        if file is not None:
            self.part_bytes += end - start
            if self.part_bytes > self.file_limit:
                # 保留同批其他文件的处理结果。其余字节仍计入整个请求的限额。
                self.oversized.add(file)
                return
            self.spooled_bytes += end - start
            self.check_bytes(self.spooled_bytes)
        super().on_part_data(data, start, end)

    def on_end(self) -> None:
        self.complete = True

    def close_files(self) -> None:
        # 截断的最后一个文件可能还未出现在 FormData 中。
        for handle in self._files_to_close_on_error:
            handle.close()


def upload_request_size(request: Request, request_limit: int) -> int:
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "multipart/form-data":
        raise HTTPException(422, "请使用 multipart/form-data 上传文件")
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise HTTPException(400, "无效的请求体长度")
        digits = length.lstrip("0") or "0"
        if len(digits) > len(str(request_limit)) or int(digits) > request_limit:
            raise HTTPException(413, "上传请求超过总大小上限，请分批上传")
        return int(digits)
    return request_limit


@asynccontextmanager
async def upload_form(request: Request, *, file_limit: int, request_limit: int,
                      check_bytes: Callable[[int], None], receive_timeout: int):
    """请求头已在预留额度前检查；按实际流入的字节执行接收限制。"""

    async def limited_stream() -> AsyncIterator[bytes]:
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > request_limit:
                raise HTTPException(413, "上传请求超过总大小上限，请分批上传")
            yield chunk

    parser = _UploadParser(request, limited_stream(), file_limit, check_bytes)
    try:
        try:
            with anyio.fail_after(receive_timeout):
                form = await parser.parse()
        except MultiPartException as exc:
            raise HTTPException(400, exc.message) from exc
        except TimeoutError as exc:
            raise HTTPException(408, "接收文件超时，请重新上传") from exc
        if not parser.complete:
            raise HTTPException(400, "上传表单不完整")
        files = form.getlist("files")
        if not files or not all(isinstance(file, UploadFile) for file in files):
            raise HTTPException(422, "files 字段必须包含上传文件")
        yield files, parser.oversized
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(parser.close_files)
