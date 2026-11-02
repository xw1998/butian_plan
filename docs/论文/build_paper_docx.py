"""Build the paper DOCX from its Markdown source.

用法：
    python build_paper_docx.py                      # 中文稿
    python build_paper_docx.py --lang en            # 英文稿
    python build_paper_docx.py --source X.md --output X.docx

设计约束（与原排版稿一致）：
  * A4 纸，左右边距 1100 twips、上下 1200 twips；
  * 三节：题录单栏 → 正文双栏（栏距 420）→ 尾部单栏；
  * 正文 9pt 宋体 + Times New Roman，标题黑体加粗，一/二/三级标题 13/11/10pt；
  * 行内与独立公式转换成 Word 原生公式（OMML），独立公式居中并右侧编号；
  * 表格为三线表；图片按栏宽内嵌；
  * 引用键 `[@Key,...]` 按首次出现顺序编号，参考文献表由 JSON 生成。

构建后自动自检：表格数、图片数、公式数、引用键解析数，任一不符即报错退出。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import latex2mathml.converter as latex2mathml
import mathml2omml
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Emu, Pt, RGBColor, Twips

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- 版式常量
PAGE_W_TWIPS = 11906  # A4 宽
PAGE_H_TWIPS = 16838  # A4 高
MARGIN_LR_TWIPS = 1100
MARGIN_TB_TWIPS = 1200
COL_SPACE_TWIPS = 420
CONTENT_W = PAGE_W_TWIPS - 2 * MARGIN_LR_TWIPS  # 9706
COL_W = (CONTENT_W - COL_SPACE_TWIPS) // 2  # 4643
MIN_COL_W = 780  # 栏内表格单列最小宽度，低于此值改为跨栏

SZ_TITLE = 34
SZ_SUBTITLE = 20
SZ_FRONT = 17  # 摘要/关键词
SZ_BODY = 18
SZ_CODE = 16
SZ_H1 = 26
SZ_H2 = 22
SZ_H3 = 20
SZ_EQNUM = 24

FONT_LATIN = "Times New Roman"
FONT_BODY_CJK = "宋体"
FONT_HEAD_CJK = "黑体"

CITE_RE = re.compile(r"\[@([^\]]+)\]")
INLINE_RE = re.compile(r"(\$[^$\n]+\$|\[@[^\]]+\]|\*\*[^*]+\*\*|`[^`]+`)")
TABLE_SEP_RE = re.compile(r"^\|[\s:\-|]+\|$")
CODE_LINE_RE = re.compile(r"^\d{2}\s{2}")
HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
IMAGE_RE = re.compile(r"^!\[[^\]]*\]\(([^)]+)\)$")


# ---------------------------------------------------------------- 工具函数
def _set_run_fonts(run, east: str) -> None:
    rpr = run._r.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = parse_xml(f"<w:rFonts {nsdecls('w')}/>")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), FONT_LATIN)
    rfonts.set(qn("w:hAnsi"), FONT_LATIN)
    rfonts.set(qn("w:cs"), FONT_LATIN)
    rfonts.set(qn("w:eastAsia"), east)


def style_run(run, size: int, *, bold: bool = False, east: str = FONT_BODY_CJK) -> None:
    _set_run_fonts(run, east)
    run.font.size = Pt(size / 2)
    run.font.color.rgb = RGBColor(0, 0, 0)
    if bold:
        run.font.bold = True
        rpr = run._r.get_or_add_rPr()
        if rpr.find(qn("w:bCs")) is None:
            rpr.append(parse_xml(f"<w:bCs {nsdecls('w')}/>"))


def omath_element(latex: str):
    """LaTeX → OMML，返回可直接挂到 w:p 下的元素。"""
    mathml = latex2mathml.convert(latex)
    omml = mathml2omml.convert(mathml)
    if "xmlns:m=" not in omml:
        omml = omml.replace(
            "<m:oMath>",
            '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">',
            1,
        )
    return parse_xml(omml)


def new_paragraph(container, *, align=None, first_line=None, before=0, after=0,
                  line=None, keep_next=False):
    p = container.add_paragraph()
    pf = p.paragraph_format
    if align is not None:
        p.alignment = align
    if first_line is not None:
        pf.first_line_indent = Twips(first_line)
    pf.space_before = Twips(before)
    pf.space_after = Twips(after)
    if line is not None:
        pf.line_spacing = line / 240.0
    if keep_next:
        pf.keep_with_next = True
        pf.keep_together = True
    return p


class Citations:
    """按首次出现顺序编号；同一括号内连续编号渲染为区间。"""

    def __init__(self, entries: dict[str, str]):
        self.entries = entries
        self.order: list[str] = []
        self.missing: set[str] = set()

    def _number(self, key: str) -> int:
        if key not in self.entries:
            self.missing.add(key)
            return 0
        if key not in self.order:
            self.order.append(key)
        return self.order.index(key) + 1

    def render(self, group: str) -> str:
        nums = sorted({self._number(k.strip()) for k in group.split(",") if k.strip()})
        if not nums:
            return ""
        if all(b - a == 1 for a, b in zip(nums, nums[1:])):
            return f"[{nums[0]}-{nums[-1]}]" if len(nums) > 1 else f"[{nums[0]}]"
        return "[" + ",".join(str(n) for n in nums) + "]"


# ---------------------------------------------------------------- 段落渲染
def add_rich_text(p, text: str, size: int, cites: Citations, *, bold=False,
                  east: str = FONT_BODY_CJK) -> None:
    """把含 $公式$ / [@引用] / **粗体** / `代码` 的文本写入段落。"""
    for token in INLINE_RE.split(text):
        if not token:
            continue
        if token.startswith("$") and token.endswith("$") and len(token) > 2:
            latex = token[1:-1].strip()
            try:
                p._p.append(omath_element(latex))
            except Exception as exc:  # pragma: no cover - 公式写错时立刻暴露
                raise RuntimeError(f"公式转换失败：{latex!r} ({exc})") from exc
        elif token.startswith("[@") and token.endswith("]"):
            m = CITE_RE.fullmatch(token)
            run = p.add_run(cites.render(m.group(1)))
            style_run(run, size, bold=bold, east=east)
        elif token.startswith("**") and token.endswith("**"):
            run = p.add_run(token[2:-2])
            style_run(run, size, bold=True, east=east)
        elif token.startswith("`") and token.endswith("`"):
            run = p.add_run(token[1:-1])
            style_run(run, size, bold=bold, east=east)
        else:
            run = p.add_run(token)
            style_run(run, size, bold=bold, east=east)


def add_display_formula(container, latex: str, number: str | None) -> None:
    """独立公式：居中公式 + 右侧编号（用 ptab 实现，与原稿一致）。"""
    p = container.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    pf = p.paragraph_format
    pf.space_before = Twips(120)
    pf.space_after = Twips(120)
    pf.line_spacing = 260 / 240.0
    pf.tab_stops.add_tab_stop(Twips(COL_W // 2), WD_TAB_ALIGNMENT.CENTER)
    pf.tab_stops.add_tab_stop(Twips(COL_W), WD_TAB_ALIGNMENT.RIGHT)

    lead = p.add_run()
    lead._r.append(parse_xml(
        f'<w:ptab {nsdecls("w")} w:alignment="center" w:relativeTo="margin" w:leader="none"/>'))
    try:
        p._p.append(omath_element(latex))
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"独立公式转换失败：{latex!r} ({exc})") from exc
    if number:
        tail = p.add_run()
        tail._r.append(parse_xml(
            f'<w:ptab {nsdecls("w")} w:alignment="right" w:relativeTo="margin" w:leader="none"/>'))
        run = p.add_run(f"({number})")
        style_run(run, SZ_EQNUM)


def add_heading(container, level: int, text: str, *, center: bool) -> None:
    if level == 1:
        style, size = "Heading 1", SZ_H1
    elif level == 2:
        style, size = "Heading 2", SZ_H2
    else:
        style, size = "Heading 3", SZ_H3
    p = new_paragraph(container, before=120, after=80, keep_next=True,
                      align=WD_ALIGN_PARAGRAPH.CENTER if center else None)
    p.style = style
    run = p.add_run(text)
    style_run(run, size, bold=True, east=FONT_HEAD_CJK)


def add_image(container, path: Path, width_emu: int = 2952750) -> None:
    p = new_paragraph(container, align=WD_ALIGN_PARAGRAPH.CENTER)
    p.add_run().add_picture(str(path), width=Emu(width_emu))


def _cell_border(cell, *, top=None, bottom=None) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    borders = tcpr.find(qn("w:tcBorders"))
    if borders is None:
        borders = parse_xml(f"<w:tcBorders {nsdecls('w')}/>")
        tcpr.append(borders)
    for side in ("top", "left", "bottom", "right"):
        el = borders.find(qn(f"w:{side}"))
        if el is None:
            el = parse_xml(f"<w:{side} {nsdecls('w')}/>")
            borders.append(el)
        if side in ("left", "right"):
            el.set(qn("w:val"), "nil")
            el.set(qn("w:sz"), "0")
        else:
            val = top if side == "top" else bottom
            if val:
                el.set(qn("w:val"), "single")
                el.set(qn("w:color"), "000000")
                el.set(qn("w:sz"), str(val))
            else:
                el.set(qn("w:val"), "nil")
                el.set(qn("w:sz"), "0")


def _cell_margins(cell) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    tcpr.append(parse_xml(
        f'<w:tcMar {nsdecls("w")}>'
        '<w:top w:w="60" w:type="dxa"/><w:left w:w="100" w:type="dxa"/>'
        '<w:bottom w:w="60" w:type="dxa"/><w:right w:w="100" w:type="dxa"/>'
        "</w:tcMar>"))


def needs_wide(rows: list[list[str]]) -> bool:
    """栏内宽度放不下最长的西文单词时，表格需要跨栏（中文字符可任意断行，不参与判定）。"""
    n_cols = len(rows[0])
    col_w = COL_W // n_cols
    if col_w < MIN_COL_W:  # 列太窄，中文只能挤成一字一行
        return True
    em = SZ_BODY * 10  # 9pt 全角宽 = 180 twips
    for row in rows:
        for cell in row:
            for token in cell.split():
                ascii_part = "".join(ch for ch in token if ord(ch) < 0x2E80)
                if not ascii_part:
                    continue
                width = sum(em if ord(ch) > 0x2E80 else em // 2 for ch in ascii_part)
                if width + 240 > col_w:  # 240 twips = 左右单元格边距 + 余量
                    return True
    return False


def add_table(container, rows: list[list[str]], cites: Citations, *, table_w: int = COL_W) -> None:
    n_rows, n_cols = len(rows), len(rows[0])
    tbl = container.add_table(rows=n_rows, cols=n_cols)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False

    tblpr = tbl._tbl.tblPr
    tblpr.append(parse_xml(
        f'<w:tblW {nsdecls("w")} w:type="dxa" w:w="{table_w}"/>'))
    col_w = table_w // n_cols

    for r_idx, row in enumerate(rows):
        for c_idx in range(n_cols):
            cell = tbl.cell(r_idx, c_idx)
            cell.width = Twips(col_w)
            is_header = r_idx == 0
            is_last = r_idx == n_rows - 1
            _cell_border(cell, top=12 if is_header else None,
                         bottom=6 if is_header else (12 if is_last else None))
            _cell_margins(cell)
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            pf = p.paragraph_format
            pf.space_after = Twips(0)
            pf.line_spacing = 320 / 240.0
            # 整表不跨页：除末行外都与下一行同页，单个行也不允许被拆开
            pf.keep_with_next = r_idx < n_rows - 1
            add_rich_text(p, row[c_idx], SZ_BODY, cites, bold=is_header)

    for r_idx, trow in enumerate(tbl.rows):
        trpr = trow._tr.get_or_add_trPr()
        trpr.append(parse_xml(f'<w:cantSplit {nsdecls("w")}/>'))
        if r_idx == 0:
            trpr.append(parse_xml(f'<w:tblHeader {nsdecls("w")}/>'))


# ---------------------------------------------------------------- Markdown 解析
def _is_ascii_only(text: str) -> bool:
    return all(ord(ch) < 128 for ch in text)


def parse_front(front: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for text in front:
        plain = text.strip().lstrip("*")
        if plain.startswith(("摘要", "Abstract")):
            out["abstract"] = text
        elif plain.startswith(("关键词", "Index Terms", "Key words")):
            out["keywords"] = text
        elif "｜" in text or " | " in text:
            out["author"] = text
        elif "title" not in out:
            out["title"] = text
        elif "subtitle" not in out and _is_ascii_only(text):
            out["subtitle"] = text
        else:
            out.setdefault("byline", text)
    return out


def iter_blocks(lines: list[str]):
    """把 md 行流切成块：('para'|'h1..h4'|'table'|'image'|'formula'|'code'|'item', payload)。"""
    buf: list[str] = []
    i = 0
    n = len(lines)

    def flush():
        nonlocal buf
        if buf:
            yield_block = ("para", " ".join(x.strip() for x in buf))
            buf = []
            return yield_block
        return None

    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            block = flush()
            if block:
                yield block
            i += 1
            continue

        # 独立公式
        if stripped.startswith("$$"):
            block = flush()
            if block:
                yield block
            chunk = stripped[2:]
            if chunk.endswith("$$"):
                body = chunk[:-2]
                i += 1
            else:
                parts = [chunk]
                i += 1
                while i < n and not lines[i].strip().endswith("$$"):
                    parts.append(lines[i])
                    i += 1
                if i < n:
                    parts.append(lines[i].strip()[:-2])
                    i += 1
                body = " ".join(parts)
            yield ("formula", body.strip())
            continue

        m = HEADING_RE.match(stripped)
        if m:
            block = flush()
            if block:
                yield block
            yield (f"h{len(m.group(1))}", m.group(2).strip())
            i += 1
            continue

        m = IMAGE_RE.match(stripped)
        if m:
            block = flush()
            if block:
                yield block
            yield ("image", m.group(1))
            i += 1
            continue

        if stripped.startswith("|"):
            block = flush()
            if block:
                yield block
            rows: list[list[str]] = []
            while i < n and lines[i].strip().startswith("|"):
                row = lines[i].strip()
                i += 1
                if TABLE_SEP_RE.match(row):
                    continue
                cells = [c.strip() for c in row.strip("|").split("|")]
                rows.append(cells)
            if rows:
                yield ("table", rows)
            continue

        if CODE_LINE_RE.match(stripped):
            block = flush()
            if block:
                yield block
            yield ("code", stripped)
            i += 1
            continue

        if stripped.startswith("- "):
            block = flush()
            if block:
                yield block
            yield ("item", stripped[2:].strip())
            i += 1
            continue

        buf.append(stripped)
        i += 1

    block = flush()
    if block:
        yield block


# ---------------------------------------------------------------- 主流程
def build(source: Path, output: Path, references: Path, lang: str) -> dict:
    text = source.read_text(encoding="utf-8")
    lines = text.splitlines()
    refs = json.loads(references.read_text(encoding="utf-8"))
    entries = {e["key"]: e["text"] for e in refs}
    cites = Citations(entries)

    blocks = list(iter_blocks(lines))
    # 前置区：文档开头直到第一个标题之前的普通段落
    front_start = 0
    title_from_heading: str | None = None
    if blocks and blocks[0][0] == "h1":
        title_from_heading = blocks[0][1]
        front_start = 1
    front_lines: list[str] = []
    cursor = front_start
    while cursor < len(blocks) and blocks[cursor][0] == "para":
        front_lines.append(blocks[cursor][1])
        cursor += 1
    front = parse_front(front_lines)
    if title_from_heading:
        front["title"] = title_from_heading

    doc = Document()
    # 文档默认字体
    normal = doc.styles["Normal"]
    normal.font.name = FONT_LATIN
    normal.font.size = Pt(SZ_BODY / 2)
    normal.paragraph_format.space_after = Twips(0)
    normal.paragraph_format.line_spacing = 260 / 240.0

    def setup_section(section, cols: int):
        section.page_width = Twips(PAGE_W_TWIPS)
        section.page_height = Twips(PAGE_H_TWIPS)
        section.left_margin = Twips(MARGIN_LR_TWIPS)
        section.right_margin = Twips(MARGIN_LR_TWIPS)
        section.top_margin = Twips(MARGIN_TB_TWIPS)
        section.bottom_margin = Twips(MARGIN_TB_TWIPS)
        section.header_distance = Twips(720)
        section.footer_distance = Twips(720)
        sectpr = section._sectPr
        cols_el = sectpr.find(qn("w:cols"))
        if cols_el is None:
            cols_el = parse_xml(f"<w:cols {nsdecls('w')}/>")
            sectpr.append(cols_el)
        cols_el.set(qn("w:num"), str(cols))
        if cols > 1:
            cols_el.set(qn("w:space"), str(COL_SPACE_TWIPS))
        else:
            cols_el.attrib.pop(qn("w:space"), None)

    setup_section(doc.sections[0], 1)

    stats = {"tables": 0, "images": 0, "display_formulas": 0, "inline_formulas": 0,
             "headings": 0, "paragraphs": 0, "references": 0}

    # ---- 前置区
    if front.get("title"):
        p = new_paragraph(doc, align=WD_ALIGN_PARAGRAPH.CENTER, after=50, line=260)
        run = p.add_run(front["title"])
        style_run(run, SZ_TITLE, bold=True)
    if front.get("subtitle"):
        p = new_paragraph(doc, align=WD_ALIGN_PARAGRAPH.CENTER, after=50, line=260)
        run = p.add_run(front["subtitle"])
        style_run(run, SZ_SUBTITLE)
    if front.get("author"):
        p = new_paragraph(doc, align=WD_ALIGN_PARAGRAPH.CENTER, after=50, line=260)
        run = p.add_run(front["author"])
        style_run(run, SZ_BODY)
    for key in ("abstract", "keywords"):
        if key not in front:
            continue
        p = new_paragraph(doc, first_line=360, after=50, line=260)
        add_rich_text(p, front[key], SZ_FRONT, cites)

    # ---- 正文（双栏）
    body_section = doc.add_section(WD_SECTION.CONTINUOUS)
    setup_section(body_section, 2)

    in_references = False
    pending_caption = None
    wide_width = 0

    def switch_section(cols: int) -> None:
        sec = doc.add_section(WD_SECTION.CONTINUOUS)
        setup_section(sec, cols)
    body = blocks[cursor:]

    # 先扫一遗：按首次出现顺序确定引用编号（参考文献表要插在正文中间）
    def block_text(kind: str, payload) -> str:
        if kind == "table":
            return " ".join(cell for row in payload for cell in row)
        if kind in ("para", "item", "code"):
            return payload
        return ""

    for kind, payload in body:
        for group in CITE_RE.findall(block_text(kind, payload)):
            cites.render(group)
    if cites.missing:
        raise SystemExit(f"引用键在参考文献库中缺失：{sorted(cites.missing)}")

    def emit_bibliography() -> None:
        for idx, key in enumerate(cites.order, start=1):
            p = new_paragraph(doc, first_line=0, after=60, line=320)
            p.paragraph_format.left_indent = Twips(420)
            p.paragraph_format.first_line_indent = Twips(-420)
            run = p.add_run(f"[{idx}] ")
            style_run(run, SZ_BODY)
            run = p.add_run(entries[key])
            style_run(run, SZ_BODY)
            stats["references"] += 1

    for b_idx, (kind, payload) in enumerate(body):
        if kind.startswith("h"):
            level = int(kind[1])
            in_references = False
            if level >= 2 and payload.strip() in ("参考文献", "References"):
                add_heading(doc, 2, payload, center=False)
                stats["headings"] += 1
                emit_bibliography()
                in_references = True
                continue
            center = level == 1 or (level == 2 and stats["headings"] == 0)
            add_heading(doc, level, payload, center=center)
            stats["headings"] += 1
            continue
        if in_references:
            continue  # 参考文献条目由 JSON 生成
        if kind == "item":
            # 正文中的无序列表（罕见）当作缩进段落
            p = new_paragraph(doc, first_line=0, after=50, line=260)
            p.paragraph_format.left_indent = Twips(360)
            add_rich_text(p, "· " + payload, SZ_BODY, cites)
            stats["paragraphs"] += 1
            continue
        if kind == "formula":
            number = None
            m = re.search(r"\\tag\{([^}]*)\}", payload)
            if m:
                number = m.group(1)
                payload = payload[: m.start()] + payload[m.end():]
            add_display_formula(doc, payload.strip(), number)
            stats["display_formulas"] += 1
            continue
        if kind == "code":
            p = new_paragraph(doc, first_line=360, after=0, line=220)
            p.paragraph_format.space_before = Twips(0)
            add_rich_text(p, payload, SZ_CODE, cites)
            stats["paragraphs"] += 1
            continue
        if kind == "image":
            add_image(doc, (source.parent / payload).resolve())
            stats["images"] += 1
            pending_caption = "figure"
            continue
        if kind == "table":
            if pending_caption == "table":
                pending_caption = None
            add_table(doc, payload, cites, table_w=wide_width or COL_W)
            if wide_width:
                switch_section(2)
                wide_width = 0
            stats["tables"] += 1
            continue
        # 普通段落：表格题注 vs 正文
        plain = payload.strip()
        is_caption = bool(re.match(r"^(表|Table)\s?\d+", plain)) or bool(
            re.match(r"^(图|Figure)\s?\d+", plain))
        if is_caption and re.match(r"^(表|Table)", plain):
            nxt = body[b_idx + 1] if b_idx + 1 < len(body) else None
            if nxt and nxt[0] == "table" and needs_wide(nxt[1]):
                switch_section(1)
                wide_width = CONTENT_W
            p = new_paragraph(doc, align=WD_ALIGN_PARAGRAPH.CENTER, before=200, after=60)
            p.paragraph_format.keep_with_next = True  # 题注不得与表格分页
            add_rich_text(p, plain, SZ_BODY, cites)
            pending_caption = "table"
            stats["paragraphs"] += 1
            continue
        p = new_paragraph(doc, first_line=360, after=50, line=260)
        add_rich_text(p, plain, SZ_BODY, cites)
        stats["paragraphs"] += 1

    if not stats["references"]:
        raise SystemExit("未找到参考文献标题（参考文献 / References）")

    # ---- 尾部单栏
    tail_section = doc.add_section(WD_SECTION.CONTINUOUS)
    setup_section(tail_section, 1)

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))

    # ---- 自检
    expected_tables = sum(1 for k, _ in blocks if k == "table")
    expected_images = sum(1 for k, _ in blocks if k == "image")
    expected_formulas = sum(1 for k, _ in blocks if k == "formula")
    problems = []
    if stats["tables"] != expected_tables:
        problems.append(f"表格 {stats['tables']} != 源 {expected_tables}")
    if stats["images"] != expected_images:
        problems.append(f"图片 {stats['images']} != 源 {expected_images}")
    if stats["display_formulas"] != expected_formulas:
        problems.append(f"独立公式 {stats['display_formulas']} != 源 {expected_formulas}")

    check = Document(str(output))
    if len(check.tables) != expected_tables:
        problems.append(f"回读表格 {len(check.tables)} != {expected_tables}")
    embedded = len(check.part.package.image_parts)
    if embedded != expected_images:
        problems.append(f"回读内嵌图片 {embedded} != {expected_images}")
    omml = check.element.body.findall(
        ".//" + qn("m:oMath"))
    if len(omml) < expected_formulas:
        problems.append(f"回读 OMML {len(omml)} < {expected_formulas}")

    stats["citations"] = len(cites.order)
    stats["omml_total"] = len(omml)
    if problems:
        raise SystemExit("自检失败：" + "；".join(problems))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="由 Markdown 生成论文 DOCX")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--lang", choices=["zh", "en"], default="zh")
    parser.add_argument("--references", type=Path,
                        default=HERE / "references" / "theme-study-references.json")
    args = parser.parse_args(argv)

    default_stem = "补天多轮经验机制" if args.lang == "zh" else "补天多轮经验机制-English"
    source = args.source or HERE / f"{default_stem}.md"
    output = args.output or HERE / f"{default_stem}.docx"

    stats = build(source, output, args.references, args.lang)
    print(f"已生成 {output.name}")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
