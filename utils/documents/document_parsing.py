"""将 Any2Markdown 的转换结果绑定到原文件页、对象和单元格。"""

from datetime import date, datetime, time
from contextlib import ExitStack, nullcontext
from decimal import Decimal
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, BadZipFile
import re
import threading

from PIL import Image, ImageSequence

from utils.documents.document_text import document, table_documents
from utils.documents.document_ocr import ocr_documents
from utils.documents.any2markdown_backend import convert_file, convert_text
from utils.runtime.shutdown import check_shutdown, interruptible_lock
from utils.indexing.progress import UnitProgressCallback

_pdf_lock = threading.Lock()


def _settings():
    from utils.core.config_loader import vector_db_config
    return vector_db_config["parsing"]


def _check_package(path):
    check_shutdown()
    # 旧版 Office 的 OLE 容器可能内嵌主题 ZIP；ZipFile 能打开它，但它不是
    # DOCX/XLSX/PPTX 包。先检查外层文件头，也涵盖使用 OLE 封装的加密文档。
    with open(path, "rb") as stream:
        if stream.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            extension = Path(path).suffix.lower()
            raise ValueError(
                f"文件实际为旧版 Office 二进制格式或加密文档，不能按未加密的 {extension} 文件解析。"
                f"请用 Office/WPS 打开后另存为未加密的 {extension} 文件再上传；仅修改扩展名无效"
            )
    try:
        with ZipFile(path) as package:
            if sum(item.file_size for item in package.infolist()) > 256 * 1024 * 1024:
                raise ValueError("Office 文件解压后超过 256 MB，请拆分后上传")
            if not {"[Content_Types].xml", "_rels/.rels"}.issubset(package.namelist()):
                raise ValueError("文件缺少有效的 Office 文档结构，请用 Office/WPS 重新另存后上传")
    except BadZipFile as exc:
        raise ValueError("Office 文件已损坏或内容与扩展名不符，请重新保存后上传") from exc


def _read_utf8(path):
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("TXT 和 Markdown 必须使用 UTF-8 编码，请转换编码后上传") from exc


def load_text(path, *, on_progress: UnitProgressCallback | None = None):
    result = document(convert_text(_read_utf8(path), "text/plain"), path)
    if on_progress is not None:
        on_progress(1, 1)
    return result


class _HTMLText(HTMLParser):
    _blocks = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "blockquote", "pre", "ul", "ol", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif (tag == "br" or tag in self._blocks) and not self.hidden:
            self.parts.append("\n")
        elif tag in ("td", "th") and not self.hidden:
            self.parts.append("\t")
        elif tag == "img" and not self.hidden:
            self.parts.append(dict(attrs).get("alt") or "")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif tag in self._blocks and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _html_text(text):
    parser = _HTMLText()
    parser.feed(text)
    return "".join(parser.parts)


def _inline_text(token):
    parser = _HTMLText()
    for child in token.children or []:
        if child.type == "html_inline":
            parser.feed(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parser.handle_data("\n")
        elif child.type == "code_inline":
            parser.handle_data(f"`{child.content}`")
        elif child.type == "image":
            parser.handle_data(f"图片说明：{child.content}")
        elif child.type == "link_open":
            parser.handle_data(f"（链接：{child.attrGet('href')}）")
        elif child.type == "text":
            parser.handle_data(child.content)
    return "".join(parser.parts)


def load_markdown(path, *, on_progress: UnitProgressCallback | None = None):
    result = markdown_documents(convert_text(_read_utf8(path), "text/markdown"), path, source_lines=True)
    if on_progress is not None:
        on_progress(1, 1)
    return result


def markdown_documents(text, source, locator="", *, source_lines=False, **metadata):
    """转换生成的行号只用于解析，引用位置始终指向实际的来源页或对象。"""
    from markdown_it import MarkdownIt

    tokens = MarkdownIt("commonmark").enable("table").parse(text)
    result, headings, parts = [], [], []
    list_numbers, list_items = [], []
    section_start = 1
    table_number = 0

    def location(detail=""):
        return " · ".join(part for part in (locator, " / ".join(value for _, value in headings), detail) if part)

    def append_part(value):
        if list_items:
            item = list_items[-1]
            prefix = item["prefix"] if item["first"] else item["indent"]
            value = prefix + value.replace("\n", "\n" + item["indent"])
            item["first"] = False
        parts.append(value)

    def flush(end):
        if parts:
            detail = f"第 {section_start}–{max(section_start, end)} 行" if source_lines else ""
            result.extend(document("\n\n".join(parts), source, location(detail), **metadata))
            parts.clear()

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type in ("ordered_list_open", "bullet_list_open"):
            if list_items and list_items[-1]["first"]:
                append_part("")
            list_numbers.append(int(token.attrs.get("start", 1)) if token.type == "ordered_list_open" else None)
        elif token.type in ("ordered_list_close", "bullet_list_close"):
            list_numbers.pop()
        elif token.type == "list_item_open":
            # 按 Markdown 渲染规则从起始编号递增；嵌套列表独立计数。
            number = list_numbers[-1]
            marker = f"{number}{token.markup} " if number is not None else f"{token.markup} "
            indent = list_items[-1]["indent"] if list_items else ""
            list_items.append({"prefix": indent + marker, "indent": indent + " " * len(marker), "first": True})
            if number is not None:
                list_numbers[-1] += 1
        elif token.type == "list_item_close":
            if list_items[-1]["first"]:
                append_part("")
            list_items.pop()
        elif token.type == "heading_open":
            flush(token.map[0])
            level = int(token.tag[1:])
            heading = _inline_text(tokens[index + 1])
            headings[:] = [(depth, value) for depth, value in headings if depth < level]
            headings.append((level, heading))
            section_start = token.map[0] + 1
        elif token.type == "table_open":
            if list_items and list_items[-1]["first"]:
                append_part("")
            context = "\n".join(parts[-2:])[-300:]
            flush(token.map[0])
            table_start, table_end = token.map
            rows = []
            while tokens[index].type != "table_close":
                current = tokens[index]
                if current.type == "tr_open":
                    rows.append([])
                elif current.type == "inline":
                    rows[-1].append(_inline_text(current))
                index += 1
            table_number += 1
            detail = f"表格（源文件第 {table_start + 1}–{table_end} 行）" if source_lines else f"表格 {table_number}"
            result.extend(table_documents(rows, source, location(detail), context=context, **metadata))
            section_start = table_end + 1
        elif token.type in ("fence", "code_block"):
            # 不清除代码内部的注释、HTML 或空白缩进。
            append_part(f"```{token.info}\n{token.content.rstrip()}\n```")
        elif token.type == "html_block":
            if re.search(r"<table\b", token.content, re.I):
                from utils.documents.html_tables import html_blocks
                context = "\n".join(parts[-2:])[-300:]
                flush(token.map[0])
                section_start = token.map[0] + 1
                for block in html_blocks(token.content):
                    if isinstance(block, str):
                        if block.strip():
                            append_part(block)
                    else:
                        if parts:
                            context = "\n".join(parts[-2:])[-300:]
                            flush(token.map[1])
                        table_number += 1
                        rows, header_rows = block
                        detail = f"表格 {table_number}"
                        if source_lines:
                            detail += f"（源文件第 {section_start}–{token.map[1]} 行）"
                        result.extend(table_documents(rows, source, location(detail),
                                                      context=context, header_rows=header_rows, **metadata))
                flush(token.map[1])
                section_start = token.map[1] + 1
            else:
                append_part(_html_text(token.content))
        elif token.type == "inline":
            # 保留链接、列表文字和图片替代文本，不抓取外部 URL。
            # Markdown 转义处理会吃掉 LaTeX 的 \( 和 \[ 分隔符。
            append_part(token.content if not source_lines and "\\" in token.content else _inline_text(token))
        index += 1
    flush(len(text.splitlines()))
    return result


def _inside(obj, bbox):
    x = (obj["x0"] + obj["x1"]) / 2
    y = (obj["top"] + obj["bottom"]) / 2
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def _render_pdf_page(path, page_index):
    import pypdfium2 as pdfium

    # PDFium 的原生调用不能跨线程重叠。释放全部原生对象后再等待 OCR 服务。
    with interruptible_lock(_pdf_lock), pdfium.PdfDocument(path) as pdf:
        page = pdf[page_index]
        try:
            width, height = page.get_size()
            scale = min(_settings()["pdf_dpi"] / 72, _settings()["ocr_max_side"] / max(width, height))
            bitmap = page.render(scale=scale)
            try:
                return bitmap.to_pil().copy(), scale
            finally:
                bitmap.close()
        finally:
            page.close()


def _pdf_images(original_page, path, page_index):
    # PDFium 渲染 CropBox，图框和文字的坐标也必须采用同一可见区域。
    media, crop = original_page.bbox, original_page.cropbox
    bounds = (max(media[0], crop[0]), max(media[1], crop[1]),
              min(media[2], crop[2]), min(media[3], crop[3]))
    page = original_page.crop(bounds)
    # 只有需要补识别的图片才需要核对重叠的原生文字。普通文本页跳过字符去重。
    text_page = None
    rendered_image = None
    result = []
    try:
        for number, img in enumerate(page.images, 1):
            check_shutdown()
            box = (max(bounds[0], img["x0"]), max(bounds[1], img["top"]),
                   min(bounds[2], img["x1"]), min(bounds[3], img["bottom"]))
            if box[2] - box[0] < 30 or box[3] - box[1] < 30:
                continue
            if rendered_image is None:
                rendered_image, scale = _render_pdf_page(path, page_index)
            pixels = tuple(round((value - bounds[i % 2]) * scale) for i, value in enumerate(box))
            if pixels[2] <= pixels[0] or pixels[3] <= pixels[1]:
                continue
            if text_page is None:
                text_page = page.dedupe_chars()
            covered = text_page.filter(lambda obj: obj.get("object_type") == "char" and _inside(obj, box))
            with rendered_image.crop(pixels) as region:
                result.extend(ocr_documents(region, path, f"图片 {number}", page=page_index + 1,
                                            existing_text=covered.extract_text() or ""))
    finally:
        if rendered_image is not None:
            rendered_image.close()
    return result


def load_pdf(path, *, on_progress: UnitProgressCallback | None = None):
    import pdfplumber

    result = []
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        if total > _settings()["max_pages"]:
            raise ValueError("PDF 页数超过解析上限，请拆分后上传")
        def converted_progress(count, _total):
            on_progress(count, total * 2)

        converted = convert_file(path, mode="auto", on_progress=converted_progress if on_progress else None)
        if [p.index for p in converted] != list(range(total)):
            raise ValueError("Any2Markdown 返回的 PDF 页码不完整")
        for entry, original_page in zip(converted, pdf.pages):
            check_shutdown()
            try:
                result.extend(markdown_documents(entry.markdown, path, "整页扫描" if entry.ocr else "正文",
                                                 extraction="ocr" if entry.ocr else "text", page=entry.index + 1))
                if not entry.ocr:
                    result.extend(_pdf_images(original_page, path, entry.index))
            finally:
                original_page.close()
            if on_progress is not None:
                on_progress(total + entry.index + 1, total * 2)
    return result


def _office_blocks(markdown, spans):
    """上游按空行连接块，依据原始块的行数区分块边界和段内空行。"""
    if not markdown:
        return {}
    lines = iter(markdown.strip().split("\n"))
    result = {}
    for i, (key, count) in enumerate(spans):
        if i and next(lines, None) != "":
            raise ValueError("Any2Markdown 的 Office 内容块与原文件位置不一致")
        block = []
        for _ in range(count):
            line = next(lines, None)
            if line is None:
                raise ValueError("Any2Markdown 返回的 Office 内容块不完整")
            block.append(line)
        content = "\n".join(block)
        result.setdefault(key, []).append(content)
    if next(lines, None) is not None:
        raise ValueError("Any2Markdown 返回了无法定位的 Office 内容块")
    return {key: "\n\n".join(blocks) for key, blocks in result.items()}


def _office_table(markdown):
    """上游 direct 转换器用带空格的竖线分列，并把格内竖线转义为 \\|。"""
    if not markdown:
        return
    lines = markdown.split("\n")
    for index, line in enumerate(lines):
        if index == 1:  # Markdown 表头分隔行
            continue
        if not line.startswith("| ") or not line.endswith(" |"):
            raise ValueError("Any2Markdown 返回的 Office 表格无法按原文件列定位")
        yield [cell.replace("\\|", "|").replace("<br>", "\n") for cell in line[2:-2].split(" | ")]


def _docx_paragraph_lines(paragraph):
    # direct 保留 run 前后的空白，纯空白 run 的首尾区间重叠，手动换行也会写两次。
    # 超链接文本会去除首尾空白，目标地址则包含在同一块中。
    pieces = []
    for item in paragraph.iter_inner_content():
        text = item.text
        if hasattr(item, "url"):
            if text.strip():
                pieces.append(f"[{text.strip()}]({item.url})" if item.url else text.strip())
        else:
            pieces.append(text * 2 if text.isspace() else text)
    content = "".join(pieces).strip()
    return content.count("\n") + 1 if content else 0


def _docx_blocks(container, path, prefix, texts, *, converted=None, cell_content=False,
                 on_progress: UnitProgressCallback | None = None):
    from docx.table import Table

    result, preceding = [], []
    blocks = list(container.iter_inner_content())
    for index, block in enumerate(blocks, 1):
        check_shutdown()
        locator = f"{prefix} · 内容块 {index}"
        markdown = converted.get(block._element) if converted else None
        if isinstance(block, Table):
            rows = []
            seen = set()
            cells = []
            converted_rows = iter(_office_table(markdown)) if markdown else iter(())
            for row_index, row in enumerate(block.rows, 1):
                check_shutdown()
                values = [None] * row.grid_cols_before
                rendered = next(converted_rows, ())
                for relative_col, cell in enumerate(row.cells):
                    if cell._tc in seen:
                        values.append("（合并单元格）")
                    else:
                        seen.add(cell._tc)
                        paragraphs = cell.paragraphs
                        native = "\n".join(texts[p._p] for p in paragraphs)
                        numbered = any(texts[p._p] != p.text for p in paragraphs)
                        # 格内真实编号与字面 <br> 只能从 OOXML 恢复。
                        use_native = (numbered or "<br>" in native or native != native.strip()
                                      or any(not p.text.strip() for p in paragraphs))
                        values.append(rendered[relative_col] if relative_col < len(rendered) and not use_native else native)
                        cells.append((cell, f"{locator} · 第 {row_index} 行第 {len(values)} 列"))
                rows.append(values + [None] * row.grid_cols_after)
            result.extend(table_documents(rows, path, f"{locator} · 表格", context="\n".join(preceding[-2:])[-300:]))
            preceding.clear()
            # 按单元格递归，文字已包含在行表示中，图片和任意层级嵌套表只访问一次。
            for cell, cell_locator in cells:
                result.extend(_docx_blocks(cell, path, cell_locator, texts, cell_content=True))
        else:
            text = texts[block._p]
            keep_whitespace = text != text.strip() or any(run.text.isspace() for run in block.runs)
            if markdown and text == block.text and not keep_whitespace:
                text = markdown
            if not cell_content:
                result.extend(document(text, path, locator))
            if text.strip():
                preceding.append(text)
            result.extend(_docx_images(block._p, container.part, path, locator))
        if on_progress is not None:
            on_progress(index, len(blocks))
    return result


def _docx_images(element, part, path, locator):
    result = []
    for number, blip in enumerate(element.xpath(".//a:blip"), 1):
        check_shutdown()
        relationship = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        if relationship is None:
            continue  # 外部链接图不下载。
        picture = part.related_parts[relationship]
        result.extend(_embedded_ocr(picture.blob, picture.content_type, path, f"{locator} · 图片 {number}"))
    return result


def _embedded_ocr(blob, content_type, path, locator):
    check_shutdown()
    # Pillow 无法稳定解码 Office 矢量图；跳过已知矢量类型，损坏的栅格图仍报错。
    if content_type in ("image/svg+xml", "image/x-emf", "image/emf", "image/x-wmf", "image/wmf"):
        return []
    with BytesIO(blob) as stream, Image.open(stream) as image:
        return ocr_documents(image, path, locator, original_stream=stream)


def load_docx(path, *, on_progress: UnitProgressCallback | None = None):
    from docx import Document
    from docx.table import Table
    from utils.documents.docx_numbering import DocxNumbering

    _check_package(path)
    doc = Document(path)
    pages = convert_file(path, mode="direct")
    if pages and (len(pages) != 1 or pages[0].index != 0):
        raise ValueError("Any2Markdown 返回的 Word 正文位置无效")
    spans = []
    for block in doc.iter_inner_content():
        if isinstance(block, Table):
            if len(block.rows) and any(row.cells for row in block.rows):
                spans.append((block._element, len(block.rows) + 1))
        else:
            lines = _docx_paragraph_lines(block)
            if lines:
                spans.append((block._element, lines))
    converted = _office_blocks(pages[0].markdown, spans) if pages else {}
    parts = [(doc, "正文")]
    seen = set()
    for index, section in enumerate(doc.sections, 1):
        for name in ("header", "first_page_header", "even_page_header", "footer", "first_page_footer", "even_page_footer"):
            part = getattr(section, name)
            if part.is_linked_to_previous:
                continue
            key = str(part.part.partname)
            if key not in seen:
                seen.add(key)
                variant = "首页" if name.startswith("first") else "偶数页" if name.startswith("even") else "默认"
                parts.append((part, f"第 {index} 节 · {variant}{'页眉' if 'header' in name else '页脚'}"))
    total = sum(sum(1 for _ in part.iter_inner_content()) for part, _ in parts)
    completed = 0
    result = []
    for part, locator in parts:
        def report_part(count: int, _part_total: int) -> None:
            if on_progress is not None:
                on_progress(completed + count, total)

        texts = DocxNumbering(doc).paragraph_texts(part)
        result.extend(_docx_blocks(part, path, locator, texts, converted=converted if part is doc else None,
                                   on_progress=report_part))
        completed += sum(1 for _ in part.iter_inner_content())
    return result


def _display_value(value, number_format):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if value is None:
        return ""
    # 仅转换语义明确的百分比和整数补零；条件、货币、缩放等格式保留存储值及格式。
    if isinstance(value, (int, float, Decimal)) and re.fullmatch(r"[0#?]+(?:\.[0#?]+)?%", number_format):
        return f"{(Decimal(str(value)) * 100).normalize():f}%"
    if isinstance(value, (int, float, Decimal)):
        if re.fullmatch(r"0+", number_format) and value == int(value):
            integer = int(value)
            return f"{'-' if integer < 0 else ''}{abs(integer):0{len(number_format)}d}"
        if number_format != "General":
            return f"{value}（存储值；数字格式 {number_format}）"
    return str(value)


def _cell_value(cell, cached_value):
    from openpyxl.worksheet.formula import DataTableFormula

    value = cell.value
    if cell.data_type == "f":
        if isinstance(value, DataTableFormula):
            formula = f"模拟运算表 {value.ref}（输入单元格 {value.r1 or '无'}, {value.r2 or '无'}）"
        else:
            formula = value if isinstance(value, str) else f"{value.text}（数组范围 {value.ref}）"
        return f"{_display_value(cached_value, cell.number_format)}（公式 {formula}；文件保存的缓存结果）" if cached_value is not None else f"{formula}（公式未保存计算结果）"
    return _display_value(value, cell.number_format)


def _open_xlsx(stack, path, *, data_only):
    from openpyxl import load_workbook

    stream = stack.enter_context(open(path, "rb"))
    try:
        book = load_workbook(stream, data_only=data_only, read_only=data_only, keep_links=False)
    except OSError as exc:
        if str(exc) != "File contains no valid workbook part":
            raise
        raise ValueError("文件中没有有效的 XLSX 工作簿，请用 Excel/WPS 重新另存后上传") from exc
    stack.callback(book.close)
    return book


def _check_sheet_bounds(sheet, available):
    """按实际工作表关系流式核对坐标和展开范围，兼容缺少 dimension 或单元格坐标的文件。"""
    from xml.parsers import expat
    from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
    from openpyxl.xml.constants import SHEET_MAIN_NS

    row = column = seen_cells = expanded = 0
    max_row = max_col = 1
    ns = SHEET_MAIN_NS + "}"
    ranges = {ns + "dimension", ns + "mergeCell", ns + "hyperlink"}

    def start(tag, attrs):
        nonlocal row, column, max_row, max_col, seen_cells, expanded
        if tag == ns + "row":
            # openpyxl 也接受将整数行号保存为 1.0 的工作簿。
            number = float(attrs.get("r", row + 1))
            if not number.is_integer():
                raise ValueError("工作表行号无效")
            row = int(number)
            column = 0
            end_row, end_col = row, 1
        elif tag == ns + "c":
            if attrs.get("r"):
                end_row, column = coordinate_to_tuple(attrs["r"])
            else:
                column += 1
                end_row = row
            end_col = column
            seen_cells += 1
        elif tag in ranges and "ref" in attrs:
            left, top, end_col, end_row = range_boundaries(attrs["ref"])
            if (None in (left, top, end_col, end_row) or not 1 <= left <= end_col
                    or not 1 <= top <= end_row):
                raise ValueError("工作表坐标范围无效")
            if tag != ns + "dimension":
                expanded += (end_col - left + 1) * (end_row - top + 1)
        else:
            return
        max_row, max_col = max(max_row, end_row), max(max_col, end_col)
        if min(end_row, end_col) < 1 or max(max_row * max_col, seen_cells, expanded) > available:
            raise ValueError("工作簿单元格范围超过解析上限，请拆分文件")

    def reject_doctype(*args):
        raise ValueError("工作表不支持 DTD 声明")

    parser = expat.ParserCreate(namespace_separator="}")
    parser.StartElementHandler = start
    parser.StartDoctypeDeclHandler = reject_doctype
    with sheet._get_source() as stream:
        try:
            parser.ParseFile(stream)
        except expat.ExpatError as exc:
            raise ValueError("工作表 XML 无效，请重新保存文件") from exc
    return max(max_row * max_col, seen_cells, expanded)


def _xlsx_documents(path, cell_limit, text_limit, progress):
    from utils.documents.xlsx_worker import TEXT_LIMIT_ERROR
    from openpyxl.drawing.spreadsheet_drawing import AbsoluteAnchor

    _check_package(path)
    with ExitStack() as stack:
        cached = _open_xlsx(stack, path, data_only=True)
        available = cell_limit
        for sheet in cached:
            available -= _check_sheet_bounds(sheet, available)
            if available < 0:
                raise ValueError("工作簿单元格范围超过解析上限，请拆分文件")
        # 只有通过全部工作表预检后，才加载保留公式、合并和图片的普通工作簿。
        book = _open_xlsx(stack, path, data_only=False)
        dimensions = [(sheet.max_row, sheet.max_column) for sheet in book]
        if sum(rows * cols for rows, cols in dimensions) > cell_limit:
            raise ValueError("工作簿单元格范围超过解析上限，请拆分文件")
        pages = convert_file(path, mode="direct")
        if pages and [p.index for p in pages] != list(range(len(book.worksheets))):
            raise ValueError("Any2Markdown 返回的工作表序号不完整")
        total = sum(rows + len(sheet._images) for sheet, (rows, _) in zip(book, dimensions))
        completed = 0
        remaining = text_limit
        for sheet_index, (sheet, (height, width)) in enumerate(zip(book, dimensions)):
            markup = pages[sheet_index].markdown if pages else ""
            table = markup.removeprefix(f"## {sheet.title}\n\n")
            converted_rows = iter(_office_table(table))
            sheet_prefix = f"工作表: {sheet.title}\n"
            prefix = sheet_prefix
            merged = {sheet.cell(area.min_row, area.min_col).coordinate: str(area)
                      for area in sheet.merged_cells.ranges}
            cached_sheet = cached[sheet.title]
            cached_rows = cached_sheet.iter_rows(max_row=height, max_col=width, values_only=True)
            for row_number, row in enumerate(sheet.iter_rows(min_row=1, max_row=height, max_col=width), 1):
                cached_row = next(cached_rows, ())
                # direct 输出省略空行，OOXML 中的原始行号仍用于定位。
                rendered = ()
                # read_only 的默认范围来自 dimension，可能小于文件中的实际范围。
                visible = cached_row[:cached_sheet.max_column]
                inside_dimension = cached_sheet.max_row is None or row_number <= cached_sheet.max_row
                if (markup and inside_dimension and
                        any(v is not None and (str(v).strip() or "\n" in str(v)) for v in visible)):
                    rendered = next(converted_rows, None)
                    if rendered is None:
                        raise ValueError("Any2Markdown 返回的工作表数据行不完整")
                values = []
                row_chars = len(prefix)
                for index, cell in enumerate(row):
                    raw = cell.value
                    plain = (isinstance(raw, str) and cell.data_type != "f" and raw == raw.strip()
                             and "<br>" not in raw) or (type(raw) in (int, float) and cell.number_format == "General")
                    if plain and index < len(rendered):
                        value = rendered[index]
                    else:
                        value = _cell_value(cell, cached_row[index] if index < len(cached_row) else None)
                    if value:
                        label = merged.get(cell.coordinate, cell.coordinate)
                        row_chars += len(label) + 2 + len(value) + bool(values)
                        if row_chars > remaining:
                            raise ValueError(TEXT_LIMIT_ERROR)
                        values.append(f"{label}: {value}")
                if values:
                    remaining -= row_chars
                    text = "；".join(values)
                    yield from document(prefix + text, path,
                                        f"工作表「{sheet.title}」 · 第 {row[0].row} 行 · {row[0].coordinate}:{row[-1].coordinate}")
                    if prefix == sheet_prefix:
                        # 复用首行前缀，复制到后续正文之前已经计入每行和整本预算。
                        prefix = f"{sheet_prefix}首个非空行: {text}\n\n"
                completed += 1
                progress(completed, total)
            if next(converted_rows, None) is not None:
                raise ValueError("Any2Markdown 返回了无法定位的工作表数据行")
            # openpyxl 暂无公开图片迭代接口；图片锚点使用工作簿内的原始位置。
            for number, picture in enumerate(sheet._images, 1):
                if isinstance(picture.anchor, AbsoluteAnchor):
                    position = f"原图坐标 {picture.anchor.pos.x},{picture.anchor.pos.y} EMU"
                else:
                    anchor = picture.anchor._from
                    position = f"第 {anchor.row + 1} 行，第 {anchor.col + 1} 列"
                with BytesIO(picture._data()) as stream, Image.open(stream) as image:
                    for doc in ocr_documents(image, path, f"工作表「{sheet.title}」 · 图片 {number}（{position}）",
                                             original_stream=stream):
                        remaining -= len(doc.page_content)
                        if remaining < 0:
                            raise ValueError(TEXT_LIMIT_ERROR)
                        yield doc
                completed += 1
                progress(completed, total)


def load_xlsx(path, *, on_progress: UnitProgressCallback | None = None):
    from utils.core.config_loader import vector_db_config
    from utils.documents.xlsx_worker import parse_isolated

    return parse_isolated(path, cell_limit=_settings()["max_sheet_cells"],
                          limits=vector_db_config["xlsx_limits"], on_progress=on_progress)


def _slide_blocks(slide, markdown):
    """转换器先输出标题，再按文件顺序输出普通对象。引用仍用页面上的对象顺序。"""
    if not markdown or markdown.startswith("<!-- slide ") and markdown.endswith(": no extractable text -->"):
        return {}
    title = slide.shapes.title
    title_id = title.shape_id if title is not None else None
    spans = []
    if title is not None and title.has_text_frame and title.text_frame.text.strip():
        spans.append((title_id, 1))
    for shape in slide.shapes:
        if shape.shape_id == title_id:
            continue
        if shape.has_text_frame:
            for para in shape.text_frame.paragraphs:
                text = "".join(run.text for run in para.runs).strip()
                if text:
                    spans.append((shape.shape_id, len(text.split("\n"))))
        if shape.has_table:
            spans.append((shape.shape_id, len(shape.table.rows) + 1))
    blocks = _office_blocks(markdown, spans)
    # direct 会折叠标题换行和内嵌软换行，用同一对象的原文恢复这些分隔符。
    for shape in slide.shapes:
        if shape.has_text_frame and shape.shape_id in blocks:
            original = shape.text_frame.text
            if "\v" in original or (shape.shape_id == title_id and "\n" in original):
                blocks[shape.shape_id] = original
    return blocks


def _slide_shapes(shapes, path, locator, converted=None):
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.shapes.picture import Picture

    result, preceding = [], []
    for index, shape in enumerate(sorted(shapes, key=lambda shape: (shape.top, shape.left)), 1):
        check_shutdown()
        where = f"{locator} · 对象 {index}"
        markdown = converted.get(shape.shape_id) if converted else None
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            result.extend(_slide_shapes(shape.shapes, path, where))
        elif shape.has_table:
            converted_rows = iter(_office_table(markdown)) if markdown else iter(())
            rows = []
            for row in shape.table.rows:
                rendered = next(converted_rows, ())
                rows.append(["（合并单元格）" if cell.is_spanned else
                             rendered[col] if col < len(rendered) and "<br>" not in cell.text else cell.text
                             for col, cell in enumerate(row.cells)])
            result.extend(table_documents(rows, path, f"{where} · 表格", context="\n".join(preceding[-2:])[-300:]))
        elif shape.has_text_frame:
            result.extend(document(markdown or "\n\n".join(p.text for p in shape.text_frame.paragraphs), path, where))
            preceding.append(shape.text_frame.text)
        elif isinstance(shape, Picture):
            # 普通图片和占位符图片共用嵌入关系，只有外部链接时跳过。
            if shape.element.blip_rId is not None:
                picture = shape.image
                result.extend(_embedded_ocr(picture.blob, picture.content_type, path, f"{where} · 图片"))
        elif shape.has_chart:
            chart = shape.chart
            if chart.has_title and chart.chart_title.has_text_frame:
                result.extend(document(chart.chart_title.text_frame.text, path, f"{where} · 图表标题"))
            # 类别图表使用原始系列数据；不从柱子高度推测数值。
            for plot_number, plot in enumerate(chart.plots, 1):
                plot_locator = f"{where} · 图表 {plot_number}"
                categories = [" / ".join(labels) for labels in plot.categories.flattened_labels]
                if plot._element.tag.rsplit("}", 1)[-1] in ("scatterChart", "bubbleChart"):
                    # 散点/气泡图没有类别轴；从 OOXML 缓存读取实际点坐标。
                    for series_number, series in enumerate(plot.series, 1):
                        axes = {}
                        names = ["xVal", "yVal"]
                        if series._element.xpath("./c:bubbleSize"):
                            names.append("bubbleSize")
                        for axis in names:
                            axes[axis] = {}
                            formats = series._element.xpath(f"./c:{axis}//c:formatCode")
                            number_format = formats[0].text if formats else "General"
                            for point in series._element.xpath(f"./c:{axis}//c:pt"):
                                value = point.find("{http://schemas.openxmlformats.org/drawingml/2006/chart}v")
                                axes[axis][int(point.get("idx"))] = (
                                    None if value is None or value.text is None
                                    else _display_value(Decimal(value.text), number_format))
                        rows = [["X", "Y", *(["气泡大小"] if "bubbleSize" in names else [])]]
                        for i in sorted(set().union(*(axis.keys() for axis in axes.values()))):
                            rows.append([axes[axis].get(i) for axis in names])
                        result.extend(table_documents(rows, path, f"{plot_locator} · 系列 {series_number}「{series.name}」",
                                                      context=f"图表系列: {series.name}"))
                    continue
                rows = [["类别", *[series.name for series in plot.series]]]
                values = []
                for series in plot.series:
                    formats = series._element.xpath("./c:val//c:formatCode")
                    number_format = formats[0].text if formats else "General"
                    values.append([_display_value(value, number_format) for value in series.values])
                for i in range(max([len(categories), *map(len, values)])):
                    rows.append([categories[i] if i < len(categories) else None,
                                 *[series[i] if i < len(series) else None for series in values]])
                result.extend(table_documents(rows, path, f"{plot_locator} · 数据"))
    return result


def load_pptx(path, *, on_progress: UnitProgressCallback | None = None):
    from pptx import Presentation

    _check_package(path)
    deck = Presentation(path)
    if len(deck.slides) > _settings()["max_pages"]:
        raise ValueError("幻灯片数量超过解析上限，请拆分后上传")
    pages = convert_file(path, mode="direct")
    if pages and [p.index for p in pages] != list(range(len(deck.slides))):
        raise ValueError("Any2Markdown 返回的幻灯片序号不完整")
    result = []
    for index, slide in enumerate(deck.slides, 1):
        check_shutdown()
        locator = f"第 {index} 张幻灯片"
        converted = _slide_blocks(slide, pages[index - 1].markdown) if pages else {}
        result.extend(_slide_shapes(slide.shapes, path, locator, converted))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            result.extend(document(slide.notes_slide.notes_text_frame.text, path, f"{locator} · 演讲者备注"))
        if on_progress is not None:
            on_progress(index, len(deck.slides))
    return result


def load_image(path, *, on_progress: UnitProgressCallback | None = None):
    result = []
    source = nullcontext(path) if hasattr(path, "read") else open(path, "rb")
    with source as stream, Image.open(stream) as image:
        frames = getattr(image, "n_frames", 1)
        if frames > _settings()["max_pages"]:
            raise ValueError("图片帧数超过解析上限，请拆分后上传")
        for index, frame in enumerate(ImageSequence.Iterator(image), 1):
            check_shutdown()
            result.extend(ocr_documents(frame, path, f"图片第 {index} 帧", original_stream=stream,
                                        cache_image=frames > 1))
            if on_progress is not None:
                on_progress(index, frames)
    if not result:
        raise ValueError("图片中未识别到文字，请上传清晰的文字图片")
    return result


LOADERS = {
    ".txt": load_text, ".md": load_markdown, ".pdf": load_pdf,
    ".docx": load_docx, ".xlsx": load_xlsx, ".pptx": load_pptx,
    **{extension: load_image for extension in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")},
}
