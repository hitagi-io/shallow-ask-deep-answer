"""各格式解析为可重复定位的文本单元；OCR 不调用远程模型。"""

from datetime import date, datetime, time
from decimal import Decimal
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, BadZipFile
from functools import wraps
import re
import threading

from PIL import Image, ImageSequence

from utils.document_text import clean_text, document, table_documents
from utils.document_ocr import ocr_documents

_pdf_lock = threading.Lock()


def _serial_pdf(function):
    # PDFium 的原生库不允许不同线程同时操作，即使它们读取不同文件。
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _pdf_lock:
            return function(*args, **kwargs)
    return wrapped


def _settings():
    from utils.config_loader import vector_db_config
    return vector_db_config["parsing"]


def _check_package(path):
    try:
        with ZipFile(path) as package:
            if sum(item.file_size for item in package.infolist()) > 256 * 1024 * 1024:
                raise ValueError("Office 文件解压后超过 256 MB，请拆分后上传")
    except BadZipFile as exc:
        raise ValueError("Office 文件已损坏或内容与扩展名不符，请重新保存后上传") from exc


def _read_utf8(path):
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("TXT 和 Markdown 必须使用 UTF-8 编码，请转换编码后上传") from exc


def load_text(path):
    return document(_read_utf8(path), path)


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


def load_markdown(path):
    from markdown_it import MarkdownIt

    text = _read_utf8(path)
    tokens = MarkdownIt("commonmark").enable("table").parse(text)
    result, headings, parts = [], [], []
    list_numbers, list_items = [], []
    section_start = 1

    def append_part(value):
        if list_items:
            item = list_items[-1]
            prefix = item["prefix"] if item["first"] else item["indent"]
            value = prefix + value.replace("\n", "\n" + item["indent"])
            item["first"] = False
        parts.append(value)

    def flush(end):
        if parts:
            label = " / ".join(value for _, value in headings)
            result.extend(document("\n\n".join(parts), path,
                                   f"{label + ' · ' if label else ''}第 {section_start}–{max(section_start, end)} 行"))
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
            label = " / ".join(value for _, value in headings)
            result.extend(table_documents(rows, path, f"{label + ' · ' if label else ''}表格（源文件第 {table_start + 1}–{table_end} 行）", context=context))
            section_start = table_end + 1
        elif token.type in ("fence", "code_block"):
            # 不清除代码内部的注释、HTML 或空白缩进。
            append_part(f"```{token.info}\n{token.content.rstrip()}\n```")
        elif token.type == "html_block":
            append_part(_html_text(token.content))
        elif token.type == "inline":
            # 保留链接、列表文字和图片替代文本，不抓取外部 URL。
            append_part(_inline_text(token))
        index += 1
    flush(len(text.splitlines()))
    return result


def _inside(obj, bbox):
    x = (obj["x0"] + obj["x1"]) / 2
    y = (obj["top"] + obj["bottom"]) / 2
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


@_serial_pdf
def load_pdf(path):
    import pdfplumber
    import pypdfium2 as pdfium

    result = []
    with pdfplumber.open(path) as pdf, pdfium.PdfDocument(path) as rendered:
        if len(pdf.pages) > _settings()["max_pages"]:
            raise ValueError("PDF 页数超过解析上限，请拆分后上传")
        for page_index, original_page in enumerate(pdf.pages):
            # PDFium 渲染 CropBox；文字提取和裁图也必须使用同一可见页面及原点。
            media, crop = original_page.bbox, original_page.cropbox
            bounds = (max(media[0], crop[0]), max(media[1], crop[1]),
                      min(media[2], crop[2]), min(media[3], crop[3]))
            page = original_page.crop(bounds).dedupe_chars()
            base = {"page": page_index + 1}
            native = clean_text(page.extract_text(x_tolerance=2, y_tolerance=3) or "")
            render_page = None
            rendered_image = None
            scale = None

            def image_region(bbox=None):
                nonlocal render_page, rendered_image, scale
                if rendered_image is None:
                    render_page = rendered[page_index]
                    width, height = render_page.get_size()
                    scale = min(_settings()["pdf_dpi"] / 72, _settings()["ocr_max_side"] / max(width, height))
                    bitmap = render_page.render(scale=scale)
                    try:
                        rendered_image = bitmap.to_pil().copy()
                    finally:
                        bitmap.close()
                if bbox:
                    x0, top, x1, bottom = bbox
                    return rendered_image.crop((round((x0 - bounds[0]) * scale), round((top - bounds[1]) * scale),
                                                round((x1 - bounds[0]) * scale), round((bottom - bounds[1]) * scale)))
                return rendered_image

            try:
                if not native:
                    result.extend(ocr_documents(image_region(), path, "整页扫描", **base))
                else:
                    tables = page.find_tables()
                    boxes = [table.bbox for table in tables]
                    # 正文排除已结构化的表格文字，防止重复召回错序表格。
                    body = page.filter(lambda obj: obj.get("object_type") != "char" or not any(_inside(obj, bbox) for bbox in boxes))
                    result.extend(document(body.extract_text(x_tolerance=2, y_tolerance=3) or "", path, "正文", **base))
                    for number, table in enumerate(tables, 1):
                        preceding = page.filter(lambda obj: obj.get("object_type") == "char" and
                                                max(bounds[1], table.bbox[1] - 90) <= obj["top"] < table.bbox[1])
                        context = (preceding.extract_text() or "")[-300:]
                        result.extend(table_documents(table.extract(), path, f"表格 {number}", context=context, **base))
                    # 少量页脚/水印不能证明图中文字已提取；OCR 后按实际文字判断是否重复。
                    for number, img in enumerate(page.images, 1):
                        box = (max(bounds[0], img["x0"]), max(bounds[1], img["top"]),
                               min(bounds[2], img["x1"]), min(bounds[3], img["bottom"]))
                        if box[2] - box[0] < 30 or box[3] - box[1] < 30:
                            continue
                        covered = page.filter(lambda obj: obj.get("object_type") == "char" and _inside(obj, box))
                        result.extend(ocr_documents(image_region(box), path, f"图片 {number}",
                                                    existing_text=covered.extract_text() or "", **base))
            finally:
                if render_page is not None:
                    render_page.close()
                if rendered_image is not None:
                    rendered_image.close()
                original_page.close()
    return result


def _docx_blocks(container, path, prefix, texts, *, cell_content=False):
    from docx.table import Table

    result, preceding = [], []
    for index, block in enumerate(container.iter_inner_content(), 1):
        locator = f"{prefix} · 内容块 {index}"
        if isinstance(block, Table):
            rows = []
            seen = set()
            cells = []
            for row_index, row in enumerate(block.rows, 1):
                values = [None] * row.grid_cols_before
                for cell in row.cells:
                    if cell._tc in seen:
                        values.append("（合并单元格）")
                    else:
                        seen.add(cell._tc)
                        values.append("\n".join(texts[paragraph._p] for paragraph in cell.paragraphs))
                        cells.append((cell, f"{locator} · 第 {row_index} 行第 {len(values)} 列"))
                rows.append(values + [None] * row.grid_cols_after)
            result.extend(table_documents(rows, path, f"{locator} · 表格", context="\n".join(preceding[-2:])[-300:]))
            preceding.clear()
            # 按单元格递归，文字已包含在行表示中，图片和任意层级嵌套表只访问一次。
            for cell, cell_locator in cells:
                result.extend(_docx_blocks(cell, path, cell_locator, texts, cell_content=True))
        else:
            text = texts[block._p]
            if not cell_content:
                result.extend(document(text, path, locator))
            if text.strip():
                preceding.append(text)
            result.extend(_docx_images(block._p, container.part, path, locator))
    return result


def _docx_images(element, part, path, locator):
    result = []
    for number, blip in enumerate(element.xpath(".//a:blip"), 1):
        relationship = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        if relationship is None:
            continue  # 外部链接图不下载。
        picture = part.related_parts[relationship]
        result.extend(_embedded_ocr(picture.blob, picture.content_type, path, f"{locator} · 图片 {number}"))
    return result


def _embedded_ocr(blob, content_type, path, locator):
    # Pillow 无法稳定解码 Office 矢量图；跳过已知矢量类型，损坏的栅格图仍报错。
    if content_type in ("image/svg+xml", "image/x-emf", "image/emf", "image/x-wmf", "image/wmf"):
        return []
    with Image.open(BytesIO(blob)) as image:
        return ocr_documents(image, path, locator)


def load_docx(path):
    from docx import Document
    from utils.docx_numbering import DocxNumbering

    _check_package(path)
    doc = Document(path)
    result = _docx_blocks(doc, path, "正文", DocxNumbering(doc).paragraph_texts(doc))
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
                texts = DocxNumbering(doc).paragraph_texts(part)
                result.extend(_docx_blocks(part, path, f"第 {index} 节 · {variant}{'页眉' if 'header' in name else '页脚'}", texts))
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


def _cell_value(cell, cached):
    from openpyxl.worksheet.formula import DataTableFormula

    value = cell.value
    if cell.data_type == "f":
        if isinstance(value, DataTableFormula):
            formula = f"模拟运算表 {value.ref}（输入单元格 {value.r1 or '无'}, {value.r2 or '无'}）"
        else:
            formula = value if isinstance(value, str) else f"{value.text}（数组范围 {value.ref}）"
        return f"{_display_value(cached.value, cell.number_format)}（公式 {formula}；文件保存的缓存结果）" if cached.value is not None else f"{formula}（公式未保存计算结果）"
    return _display_value(value, cell.number_format)


def load_xlsx(path):
    from openpyxl import load_workbook
    from openpyxl.drawing.spreadsheet_drawing import AbsoluteAnchor

    _check_package(path)
    book = load_workbook(path, data_only=False, keep_links=False)
    result = []
    try:
        cached = load_workbook(path, data_only=True, keep_links=False)
    except Exception:
        book.close()
        raise
    try:
        if sum(sheet.max_row * sheet.max_column for sheet in book) > _settings()["max_sheet_cells"]:
            raise ValueError("工作簿单元格范围超过解析上限，请删除多余空白行列或拆分文件")
        for sheet in book:
            first = ""
            merged = {sheet.cell(area.min_row, area.min_col).coordinate: str(area)
                      for area in sheet.merged_cells.ranges}
            for row in sheet.iter_rows():
                values = []
                for cell in row:
                    value = _cell_value(cell, cached[sheet.title][cell.coordinate])
                    if value:
                        merge = merged.get(cell.coordinate)
                        values.append(f"{merge or cell.coordinate}: {value}")
                if not values:
                    continue
                text = "；".join(values)
                prefix = f"工作表: {sheet.title}\n"
                if first:
                    prefix += f"首个非空行: {first}\n\n"
                else:
                    first = text
                result.extend(document(prefix + text, path,
                                       f"工作表「{sheet.title}」 · 第 {row[0].row} 行 · {row[0].coordinate}:{row[-1].coordinate}"))
            # openpyxl 暂无公开图片迭代接口；图片锚点使用工作簿内的原始位置。
            for number, picture in enumerate(sheet._images, 1):
                if isinstance(picture.anchor, AbsoluteAnchor):
                    position = f"原图坐标 {picture.anchor.pos.x},{picture.anchor.pos.y} EMU"
                else:
                    anchor = picture.anchor._from
                    position = f"第 {anchor.row + 1} 行，第 {anchor.col + 1} 列"
                with Image.open(BytesIO(picture._data())) as image:
                    result.extend(ocr_documents(image, path, f"工作表「{sheet.title}」 · 图片 {number}（{position}）"))
    finally:
        book.close()
        cached.close()
    return result


def _slide_shapes(shapes, path, locator):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    result, preceding = [], []
    for index, shape in enumerate(sorted(shapes, key=lambda shape: (shape.top, shape.left)), 1):
        where = f"{locator} · 对象 {index}"
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            result.extend(_slide_shapes(shape.shapes, path, where))
        elif shape.has_table:
            rows = [["（合并单元格）" if cell.is_spanned else cell.text for cell in row.cells] for row in shape.table.rows]
            result.extend(table_documents(rows, path, f"{where} · 表格", context="\n".join(preceding[-2:])[-300:]))
        elif shape.has_text_frame:
            result.extend(document("\n\n".join(paragraph.text for paragraph in shape.text_frame.paragraphs), path, where))
            preceding.append(shape.text_frame.text)
        elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            result.extend(_embedded_ocr(shape.image.blob, shape.image.content_type, path, f"{where} · 图片"))
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


def load_pptx(path):
    from pptx import Presentation

    _check_package(path)
    deck = Presentation(path)
    if len(deck.slides) > _settings()["max_pages"]:
        raise ValueError("幻灯片数量超过解析上限，请拆分后上传")
    result = []
    for index, slide in enumerate(deck.slides, 1):
        locator = f"第 {index} 张幻灯片"
        result.extend(_slide_shapes(slide.shapes, path, locator))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            result.extend(document(slide.notes_slide.notes_text_frame.text, path, f"{locator} · 演讲者备注"))
    return result


def load_image(path):
    result = []
    with Image.open(path) as image:
        if getattr(image, "n_frames", 1) > _settings()["max_pages"]:
            raise ValueError("图片帧数超过解析上限，请拆分后上传")
        for index, frame in enumerate(ImageSequence.Iterator(image), 1):
            result.extend(ocr_documents(frame, path, f"图片第 {index} 帧"))
    if not result:
        raise ValueError("图片中未识别到文字，请上传清晰的文字图片")
    return result


LOADERS = {
    ".txt": load_text, ".md": load_markdown, ".pdf": load_pdf,
    ".docx": load_docx, ".xlsx": load_xlsx, ".pptx": load_pptx,
    **{extension: load_image for extension in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")},
}
