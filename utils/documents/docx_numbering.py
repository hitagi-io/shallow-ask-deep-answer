"""按 Word 编号定义还原段落前缀；编号状态只属于当前正文或页眉页脚。"""

from dataclasses import dataclass, replace
import re

from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.table import Table


def _value(element, path, default=None):
    child = element.find(path) if element is not None else None
    return child.get(qn("w:val"), default) if child is not None else default


def _path(*names):
    return "/".join(qn(f"w:{name}") for name in names)


@dataclass(frozen=True)
class _Level:
    start: int
    format: str
    text: str
    restart: int
    suffix: str
    legal: bool
    style: str | None


def _level(element, index):
    restart = int(_value(element, _path("lvlRestart"), index))
    text_element = element.find(qn("w:lvlText"))
    text = _value(element, _path("lvlText"), "")
    if text_element is not None and text_element.get(qn("w:null")) in ("1", "true", "on"):
        text = ""
    legal = element.find(qn("w:isLgl"))
    return _Level(
        start=int(_value(element, _path("start"), 0)),
        format=_value(element, _path("numFmt"), "decimal"),
        text=text,
        restart=restart if 0 <= restart <= index else index,
        suffix={"tab": "\t", "space": " ", "nothing": ""}.get(_value(element, _path("suff"), "tab"), "\t"),
        legal=legal is not None and legal.get(qn("w:val"), "1") not in ("0", "false", "off"),
        style=_value(element, _path("pStyle")),
    )


def _number(value, kind):
    if kind == "decimal":
        return str(value)
    if kind == "decimalZero":
        return f"{'-' if value < 0 else ''}{abs(value):02d}"
    if kind in ("upperLetter", "lowerLetter") and 0 < value <= 26 * 256:
        # Word 的字母编号在 Z 后依次为 AA、BB，而不是电子表格列名的 AA、AB。
        letter = chr(ord("A") + (value - 1) % 26) * ((value - 1) // 26 + 1)
        return letter.lower() if kind == "lowerLetter" else letter
    if kind in ("upperRoman", "lowerRoman") and 0 < value < 4000:
        result = ""
        for amount, symbol in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
                               (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
                               (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
            count, value = divmod(value, amount)
            result += symbol * count
        return result.lower() if kind == "lowerRoman" else result
    if kind == "none":
        return ""
    # 未实现的语言、字形或超出范围的格式保留序号并明确标注，不伪造显示值。
    return f"{value}（编号格式 {kind}）"


class DocxNumbering:
    def __init__(self, doc):
        try:
            root = doc.part.part_related_by(RT.NUMBERING).element
        except KeyError:
            root = None  # 普通文档可以没有 numbering.xml；读取时不创建部件。
        self.numbers = {int(item.get(qn("w:numId"))): item for item in root.findall(qn("w:num"))} if root is not None else {}
        self.abstracts = {int(item.get(qn("w:abstractNumId"))): item for item in root.findall(qn("w:abstractNum"))} if root is not None else {}
        self.styles = {item.style_id: item.element for item in doc.styles}
        self.levels = {}
        self.counters = {}

    def _style_chain(self, style_id):
        seen = set()
        while style_id in self.styles:
            if style_id in seen:
                raise ValueError("DOCX 段落样式存在循环引用，请重新保存文件后上传")
            seen.add(style_id)
            style = self.styles[style_id]
            yield style_id, style
            style_id = _value(style, _path("basedOn"))

    def _levels(self, num_id, seen=()):
        if num_id in self.levels:
            return self.levels[num_id]
        if num_id in seen:
            raise ValueError("DOCX 编号样式存在循环引用，请重新保存文件后上传")
        number = self.numbers.get(num_id)
        abstract_id = _value(number, _path("abstractNumId"))
        abstract = self.abstracts.get(int(abstract_id)) if abstract_id is not None else None
        if abstract is None:
            raise ValueError(f"DOCX 缺少编号 {num_id} 的定义，请重新保存文件后上传")
        linked_style = _value(abstract, _path("numStyleLink"))
        levels = {}
        if linked_style is not None:
            linked_num = next((value for _, style in self._style_chain(linked_style)
                               if (value := _value(style, _path("pPr", "numPr", "numId"))) is not None), None)
            if linked_num is None:
                raise ValueError("DOCX 编号样式缺少目标编号，请重新保存文件后上传")
            levels.update(self._levels(int(linked_num), (*seen, num_id)))
        for element in abstract.findall(qn("w:lvl")):
            index = int(element.get(qn("w:ilvl")))
            levels[index] = _level(element, index)
        for override in number.findall(qn("w:lvlOverride")):
            index = int(override.get(qn("w:ilvl")))
            element = override.find(qn("w:lvl"))
            if element is not None:
                replacement = _level(element, index)
                # Word 使用抽象定义中的重启规则，忽略覆盖层内的 lvlRestart。
                levels[index] = replace(replacement, restart=levels[index].restart if index in levels else index)
            start = _value(override, _path("startOverride"))
            if start is not None and index in levels:
                levels[index] = replace(levels[index], start=int(start))
        self.levels[num_id] = levels
        return levels

    def text(self, paragraph):
        style = paragraph.style
        styles = list(self._style_chain(style.style_id if style is not None else None))
        properties = [paragraph._p, *(style for _, style in styles)]
        num_id = next((value for element in properties
                       if (value := _value(element, _path("pPr", "numPr", "numId"))) is not None), None)
        if num_id is None or int(num_id) == 0:
            return paragraph.text
        num_id = int(num_id)
        levels = self._levels(num_id)
        index = _value(paragraph._p, _path("pPr", "numPr", "ilvl"))
        if index is None:
            # 编号层级优先取段落直接设置，其次取段落样式与编号定义的关联。
            index = next((level for style_id, _ in styles for level, definition in levels.items()
                          if definition.style == style_id), None)
        if index is None:
            index = next((value for _, style in styles
                          if (value := _value(style, _path("pPr", "numPr", "ilvl"))) is not None), 0)
        index = int(index)
        if index not in levels or not 0 <= index <= 8:
            raise ValueError(f"DOCX 编号 {num_id} 缺少有效的第 {index + 1} 级定义")
        definition = levels[index]
        counters = self.counters.setdefault(num_id, {})
        counters[index] = counters[index] + 1 if index in counters else definition.start
        # 重启点是从 1 开始的层级号；0 表示永不重启，更高层级也会触发 Word 重启。
        for child, child_definition in levels.items():
            if child > index and child_definition.restart > index:
                counters.pop(child, None)

        def substitute(match):
            reference = int(match[1]) - 1
            if reference > index:
                return ""
            if reference not in levels:
                raise ValueError("DOCX 编号文本引用了不存在的层级")
            level = levels[reference]
            return _number(counters.get(reference, level.start), "decimal" if definition.legal else level.format)

        marker = definition.text if definition.format == "bullet" else re.sub(r"%([1-9])", substitute, definition.text)
        if definition.format == "none":
            marker = ""
        if definition.format == "bullet" and any("\ue000" <= char <= "\uf8ff" for char in marker):
            marker = "•"  # 字体私用区的项目符号以可读的通用圆点表示。
        return f"{'  ' * index}{marker}{definition.suffix}{paragraph.text}" if marker else paragraph.text

    def paragraph_texts(self, container):
        """先按文档顺序计算一次，避免表格行和递归单元格重复推进编号。"""
        result = {}
        for block in container.iter_inner_content():
            if isinstance(block, Table):
                seen = set()
                for row in block.rows:
                    for cell in row.cells:
                        if cell._tc not in seen:
                            seen.add(cell._tc)
                            result.update(self.paragraph_texts(cell))
            else:
                result[block._p] = self.text(block)
        return result
