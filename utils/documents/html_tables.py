"""将转换器的 HTML 表格还原成有界的行列网格，保留合并格的位置。"""

from lxml import html

_MAX_CELLS = 250_000
_MERGED = "（合并单元格）"


def _rows(table):
    rows = []
    header_rows = 0
    occupied_until = {}
    total = 0
    elements = table.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr")
    for index, row in enumerate(elements):
        if (index == header_rows and (row.getparent().tag == "thead" or
                                      (row.find("th") is not None and row.find("td") is None))):
            header_rows += 1
        occupied_until = {key: until for key, until in occupied_until.items() if until > index}
        cells = []
        column = 0
        for cell in row.xpath("./td|./th"):
            while occupied_until.get(column, 0) > index:
                cells.append(_MERGED)
                column += 1
            try:
                width = int(cell.get("colspan", "1"))
                height = int(cell.get("rowspan", "1"))
            except ValueError as exc:
                raise ValueError("转换结果中的表格跨度无效") from exc
            if height == 0:  # HTML 的 rowspan=0 延伸到行组末端。
                height = 1 + len(row.xpath("following-sibling::tr"))
            if min(width, height) < 1 or total + column + width > _MAX_CELLS:
                raise ValueError("转换结果中的表格超出行列上限")
            for offset in range(width):
                if occupied_until.get(column, 0) > index:
                    raise ValueError("转换结果中的合并单元格重叠")
                cells.append("".join(cell.itertext()).strip() if offset == 0 else _MERGED)
                occupied_until[column] = index + height
                column += 1
        end = max((key + 1 for key, until in occupied_until.items() if until > index), default=column)
        if total + end > _MAX_CELLS:
            raise ValueError("转换结果中的表格超出行列上限")
        cells.extend(_MERGED if occupied_until.get(key, 0) > index else "" for key in range(column, end))
        total += len(cells)
        rows.append(cells)
    return rows, header_rows


def html_blocks(fragment):
    root = html.fragment_fromstring(fragment, create_parent="div")
    for node in root.xpath(".//script|.//style"):
        node.drop_tree()
    for node in root.iter("br"):
        node.tail = "\n" + (node.tail or "")
    parts = []

    def walk(node):
        if not isinstance(node.tag, str):
            return
        if node.tag == "table":
            if parts:
                yield "".join(parts)
                parts.clear()
            yield _rows(node)
            return
        if node.tag in ("div", "p", "li", "h1", "h2", "h3"):
            parts.append("\n")
        parts.append(node.text or "")
        for child in node:
            yield from walk(child)
            parts.append(child.tail or "")

    yield from walk(root)
    if parts:
        yield "".join(parts)
