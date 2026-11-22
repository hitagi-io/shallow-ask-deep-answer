"""在单独进程中解析工作簿，按行传回进度和有界的文档结果。"""
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import redirect_stdout
from contextvars import copy_context
import json
import os
import subprocess
import sys
import threading
import time

from utils.core.project_paths import get_project_root
from utils.runtime.shutdown import check_shutdown, interruptible_lock

_slot = threading.Lock()
TEXT_LIMIT_ERROR = "工作簿提取文本超过上限，请拆分后上传"
_OUTPUT_LIMIT_ERROR = "工作簿提取结果及定位信息过多，请拆分后上传"
_PROCESS_ERROR = "工作簿解析进程异常结束或超过内存上限，请拆分后上传"


def _read_documents(stream, path, text_limit, on_progress):
    from langchain_core.documents import Document
    from utils.documents.document_ocr import include_ocr_counts

    documents = []
    remaining_chars = text_limit
    # 包含 JSON 转义、定位元数据和文档对象开销，限制父进程接收的总量。
    remaining_bytes = text_limit * 8 + 65536
    while line := stream.readline(remaining_bytes + 1):
        remaining_bytes -= len(line)
        if remaining_bytes < 0:
            raise ValueError(_OUTPUT_LIMIT_ERROR)
        event, *data = json.loads(line)
        if event == "done":
            include_ocr_counts(*data)
            return documents
        if event == "error":
            raise ValueError(data[0])
        if event == "progress":
            if on_progress is not None:
                on_progress(*data)
        elif event == "document":
            content, metadata = data
            remaining_chars -= len(content)
            remaining_bytes -= 256  # 每个 Python Document 还有固定开销。
            if remaining_chars < 0:
                raise ValueError(TEXT_LIMIT_ERROR)
            if remaining_bytes < 0:
                raise ValueError(_OUTPUT_LIMIT_ERROR)
            documents.append(Document(page_content=content, metadata={**metadata, "source": path}))
        else:
            raise ValueError("工作簿解析结果无效")
    raise ValueError(_PROCESS_ERROR)


def parse_isolated(path, *, cell_limit, limits, on_progress):
    deadline = time.monotonic() + limits["timeout_seconds"]

    def time_left():
        check_shutdown()
        left = deadline - time.monotonic()
        if left <= 0:
            raise ValueError("工作簿解析超时，请拆分后上传")
        return left

    with interruptible_lock(_slot, cancel_check=time_left):
        # 固定原生数学库的线程数，避免线程栈预留耗尽子进程地址空间。
        env = {**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
        command = [sys.executable, *subprocess._args_from_interpreter_flags(), "-m", "utils.documents.xlsx_worker",
                   os.path.abspath(path), str(cell_limit), str(limits["max_text_chars"]), str(limits["memory_mb"])]
        with subprocess.Popen(command, cwd=get_project_root(), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as process, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="xlsx-output") as reader:
            try:
                result = reader.submit(copy_context().run, _read_documents, process.stdout, str(path),
                                       limits["max_text_chars"], on_progress)
                while not result.done():
                    wait([result], timeout=min(0.2, time_left()))
                documents = result.result()
                if process.wait(timeout=time_left()) != 0:
                    raise ValueError(_PROCESS_ERROR)
                return documents
            except subprocess.TimeoutExpired as exc:
                raise ValueError("工作簿解析超时，请拆分后上传") from exc
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()


def _main():
    path, cells, chars, memory = sys.argv[1:]
    output = sys.stdout.buffer

    def send(*event):
        output.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")

    last_percent = -1

    def progress(completed, total):
        nonlocal last_percent
        percent = completed * 100 // total
        if percent != last_percent:
            send("progress", completed, total)
            output.flush()
            last_percent = percent

    with redirect_stdout(sys.stderr):
        try:
            from utils.runtime.process_memory import limit_process_memory
            limit_process_memory(int(memory) * 1024**2)
            from utils.documents.document_parsing import _xlsx_documents
            from utils.documents.document_ocr import document_ocr_progress

            with document_ocr_progress(path) as stats:
                for doc in _xlsx_documents(path, int(cells), int(chars), progress):
                    metadata = {k: v for k, v in doc.metadata.items() if k != "source"}
                    send("document", doc.page_content, metadata)
            send("done", stats.count, stats.empty, stats.skipped, stats.reused)
        except MemoryError:
            send("error", _PROCESS_ERROR)
        except ValueError as exc:
            send("error", str(exc)[:512])
        except Exception:
            import logging
            logging.getLogger(__name__).exception("工作簿解析失败")
            send("error", "无法解析工作簿，请检查文件或查看服务端日志")
        output.flush()


if __name__ == "__main__":
    _main()
