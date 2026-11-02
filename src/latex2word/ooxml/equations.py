"""Numbered display equations: equation centred, number at the side (publisher-neutral).

Pandoc writes a numbered display equation as two sibling paragraphs, an
``Equation`` paragraph holding ``m:oMathPara`` and an ``EquationNumber``
paragraph holding ``(n)``. Two layouts are supported (see
``docs/equation-layout.md`` for the trade-off analysis):

``table`` (default)
    One borderless single-row, three-cell table ``[side | equation | side]``.
    Both outer cells have the *same* width (``layout.equations.number_width``),
    so the equation is centred on the column whatever the number's width. The
    original paragraphs are *moved*, never rebuilt: the native OMML,
    its bookmarks and the cross-reference anchors survive byte for byte.
    The table uses Word's autofit layout with these preferred widths and a
    no-wrap number cell: an equation up to ``column - number_width`` wide is
    shown whole (Word takes the extra width from the empty spacer cell); only a
    wider one is clipped at the cell edge.

``tabs``
    One paragraph ``<tab> inline-math <tab> (n)`` with a centre tab at half the
    column and a right tab at its edge. Drawback: math in a paragraph that mixes
    text and math is *inline* math, which Word renders in the compact "in-line"
    style (fractions shrunk, limits at the side) rather than the display style of
    a stand-alone ``m:oMathPara``; a multi-line equation cannot be expressed at
    all, so such an equation falls back to the table layout.

Unpaired display equations (no ``EquationNumber`` follows) are left untouched,
and the pass is idempotent: transformed equations no longer match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lxml import etree

from latex2word.diagnostics import Severity
from latex2word.ooxml import style_ids
from latex2word.ooxml.namespaces import qn, text_of, w, xpath
from latex2word.ooxml.sections import SectionPlan
from latex2word.ooxml.xmlutil import PPR_ORDER, TBLPR_ORDER, TCPR_ORDER, TRPR_ORDER, ensure_child, paragraph_style, ppr, set_child
from latex2word.source_graph import Finding
from latex2word.units import Length

MIN_MIDDLE = Length.inches("1.0")  # narrower than this an equation cannot be shown in its cell
MIN_SIDE = Length.inches("0.25")
_BOOKMARKS = (w("bookmarkStart"), w("bookmarkEnd"))


@dataclass(frozen=True)
class EquationOutcome:
    """The layout applied to one numbered equation (widths in twips; ``None`` in ``tabs`` mode)."""

    number: str
    mode: str
    section: str  # frontmatter | body
    column_width_twips: int
    side_width_twips: int | None
    middle_width_twips: int | None
    has_omml: bool
    bookmark_names: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number, "mode": self.mode, "section": self.section,
            "column_width_twips": self.column_width_twips, "side_width_twips": self.side_width_twips,
            "middle_width_twips": self.middle_width_twips, "has_omml": self.has_omml,
            "bookmark_names": list(self.bookmark_names),
        }


def _finding(severity: Severity, number: int, message: str) -> Finding:
    return Finding(f"L2W-{severity.value[0].upper()}-LAYOUT-{number:03d}", severity, message)


# --------------------------------------------------------------------------- discovery


def _in_equation_table(paragraph: etree._Element) -> bool:
    cell = paragraph.getparent()
    table = cell.getparent().getparent() if cell is not None and cell.tag == w("tc") else None
    if table is None or table.tag != w("tbl"):
        return False
    style = table.find(f"{w('tblPr')}/{w('tblStyle')}")
    return style is not None and style.get(w("val")) == style_ids.TABLE_EQUATION_LAYOUT


def _pairs(document: etree._Element) -> list[tuple[etree._Element, etree._Element]]:
    """``(Equation paragraph, EquationNumber paragraph)`` pairs not yet transformed."""
    pairs = []
    for paragraph in document.iter(w("p")):
        if paragraph_style(paragraph) != style_ids.EQUATION or paragraph.find(qn("m:oMathPara")) is None:
            continue
        following = paragraph.getnext()
        if following is None or following.tag != w("p") or paragraph_style(following) != style_ids.EQUATION_NUMBER:
            continue
        if not _in_equation_table(paragraph):
            pairs.append((paragraph, following))
    return pairs


def _available_twips(paragraph: etree._Element, plan: SectionPlan) -> int:
    """Column width, or the fixed width of the table cell the equation sits in."""
    parent = paragraph.getparent()
    if parent is not None and parent.tag == w("tc"):
        cell_width = parent.find(f"{w('tcPr')}/{w('tcW')}")
        if cell_width is not None and cell_width.get(w("type")) == "dxa":
            return int(cell_width.get(w("w"), "0"))
    return plan.geometry_for(paragraph).column_width.twips


def _bookmark_names(paragraph: etree._Element) -> tuple[str, ...]:
    return tuple(node.get(w("name"), "") for node in paragraph.iter(w("bookmarkStart")))


# --------------------------------------------------------------------------- building blocks


def _spacer_paragraph() -> etree._Element:
    paragraph = etree.Element(w("p"))
    set_child(ppr(paragraph), "pStyle", PPR_ORDER, val=style_ids.EQUATION_SPACER)
    return paragraph


def _guard_paragraph() -> etree._Element:
    """Smallest paragraph Word allows (1 pt line): keeps two tables from being merged into one."""
    paragraph = etree.Element(w("p"))
    properties = ppr(paragraph)
    set_child(properties, "spacing", PPR_ORDER, before="0", after="0", line="20", lineRule="exact")
    run_properties = ensure_child(properties, "rPr", PPR_ORDER)
    etree.SubElement(run_properties, w("sz")).set(w("val"), "2")
    return paragraph


def _cell(width: int, content: etree._Element, *, no_wrap: bool = False) -> etree._Element:
    cell = etree.Element(w("tc"))
    properties = etree.SubElement(cell, w("tcPr"))
    set_child(properties, "tcW", TCPR_ORDER, w=str(width), type="dxa")
    if no_wrap:  # the number never breaks "(3.45)" over two lines when Word redistributes the columns
        set_child(properties, "noWrap", TCPR_ORDER)
    set_child(properties, "vAlign", TCPR_ORDER, val="center")
    cell.append(content)
    return cell


def _table(
    total: int, widths: tuple[int, int, int], contents: tuple[etree._Element, ...], number_index: int | None = None
) -> etree._Element:
    table = etree.Element(w("tbl"))
    properties = etree.SubElement(table, w("tblPr"))
    set_child(properties, "tblStyle", TBLPR_ORDER, val=style_ids.TABLE_EQUATION_LAYOUT)
    set_child(properties, "tblW", TBLPR_ORDER, w=str(total), type="dxa")
    set_child(properties, "jc", TBLPR_ORDER, val="center")
    # autofit with preferred widths: an equation that fits keeps its centred cell; a wider one takes width
    # from the empty spacer cell instead of being clipped (a fixed layout clips it at the cell edge)
    set_child(properties, "tblLayout", TBLPR_ORDER, type="autofit")
    set_child(
        properties, "tblLook", TBLPR_ORDER,
        val="0000", firstRow="0", lastRow="0", firstColumn="0", lastColumn="0", noHBand="1", noVBand="1",
    )
    grid = etree.SubElement(table, w("tblGrid"))
    for width in widths:
        etree.SubElement(grid, w("gridCol")).set(w("w"), str(width))
    row = etree.SubElement(table, w("tr"))
    set_child(etree.SubElement(row, w("trPr")), "cantSplit", TRPR_ORDER)
    for index, (width, content) in enumerate(zip(widths, contents, strict=True)):
        row.append(_cell(width, content, no_wrap=index == number_index))
    return table


def _side_widths(available: int, wanted: Length, label: str, findings: list[Finding]) -> tuple[int, int]:
    """``(side, middle)`` in twips; sides shrink symmetrically (floor 0.25in) when the middle gets too narrow."""
    side = wanted.twips
    if available - 2 * side >= MIN_MIDDLE.twips:
        return side, available - 2 * side
    reduced = max(MIN_SIDE.twips, (available - MIN_MIDDLE.twips) // 2)
    reduced = min(reduced, side)
    findings.append(
        _finding(
            Severity.WARNING, 31,
            f"equation {label}: column of {available} twips leaves less than {MIN_MIDDLE.twips} twips for the equation "
            f"with number width {side} twips; number width reduced to {reduced} twips",
        )
    )
    return reduced, available - 2 * reduced


def _align_left(equation: etree._Element, number: etree._Element, align_equation: bool, align_number: bool) -> None:
    """Direct alignment where the profile deviates from the styles' centre / right defaults."""
    if align_equation:
        set_child(ppr(equation), "jc", PPR_ORDER, val="left")
        for jc in equation.findall(f"{qn('m:oMathPara')}/{qn('m:oMathParaPr')}/{qn('m:jc')}"):
            jc.set(qn("m:val"), "left")
    if align_number:
        set_child(ppr(number), "jc", PPR_ORDER, val="left")


def _ensure_separated(table: etree._Element) -> None:
    """A guard paragraph wherever Word would merge the table with a neighbouring table or has no paragraph to end on."""
    if table.getparent() is None:
        return
    before = table.getprevious()
    while before is not None and before.tag in _BOOKMARKS:
        before = before.getprevious()
    if before is not None and before.tag == w("tbl"):
        table.addprevious(_guard_paragraph())
    after = table.getnext()
    while after is not None and after.tag in _BOOKMARKS:
        after = after.getnext()
    if after is None or after.tag in (w("tbl"), w("sectPr")):
        table.addnext(_guard_paragraph())


def _table_layout(
    equation: etree._Element, number: etree._Element, plan: SectionPlan, label: str, findings: list[Finding]
) -> tuple[int, int, int]:
    layout = plan.layout.equations
    available = _available_twips(equation, plan)
    side, middle = _side_widths(available, layout.number_width, label, findings)
    _align_left(equation, number, layout.alignment == "left", layout.number_alignment == "left")
    parent = equation.getparent()
    position = parent.index(equation)
    tail = number.tail
    contents = (
        (number, equation, _spacer_paragraph())
        if layout.number_alignment == "left"
        else (_spacer_paragraph(), equation, number)
    )
    number_index = 0 if layout.number_alignment == "left" else 2
    table = _table(available, (side, middle, side), contents, number_index)  # moves the original paragraphs into the cells
    parent.insert(position, table)
    table.tail = tail
    for paragraph in (equation, number):
        paragraph.tail = None
    _ensure_separated(table)
    return available, side, middle


def _tabs_layout(equation: etree._Element, number: etree._Element, available: int, number_left: bool) -> None:
    math_para = equation.find(qn("m:oMathPara"))
    inline = [child for child in math_para if child.tag == qn("m:oMath")]
    number_content = [child for child in number if child.tag != w("pPr")]
    index = equation.index(math_para)
    equation.remove(math_para)
    if number_left:
        placed = [*number_content, _tab_run(), *inline]
    else:
        placed = [_tab_run(), *inline, _tab_run()]
    for offset, child in enumerate(placed):
        equation.insert(index + offset, child)
    if not number_left:
        equation.extend(number_content)
    properties = ppr(equation)
    tabs = set_child(properties, "tabs", PPR_ORDER)
    for stale in list(tabs):
        tabs.remove(stale)
    stops = [("center", available // 2)] if number_left else [("center", available // 2), ("right", available)]
    for kind, position in stops:
        stop = etree.SubElement(tabs, w("tab"))
        stop.set(w("val"), kind)
        stop.set(w("pos"), str(position))
    set_child(properties, "jc", PPR_ORDER, val="left")
    number.getparent().remove(number)


def _tab_run() -> etree._Element:
    run = etree.Element(w("r"))
    etree.SubElement(run, w("tab"))
    return run


def _section_name(equation: etree._Element, plan: SectionPlan) -> str:
    return "frontmatter" if plan.is_frontmatter(equation) else "body"


def apply_equation_layout(document: etree._Element, plan: SectionPlan) -> tuple[list[EquationOutcome], list[Finding]]:
    """Lay out every paired display equation per ``layout.equations.mode`` (see module docstring)."""
    layout = plan.layout.equations
    outcomes: list[EquationOutcome] = []
    findings: list[Finding] = []
    for equation, number in _pairs(document):
        number_text = text_of(number).strip()
        label = repr(number_text)
        names = _bookmark_names(equation)
        section = _section_name(equation, plan)
        mode = layout.mode
        if mode == "tabs" and len(xpath(equation, "./m:oMathPara/m:oMath")) != 1:
            findings.append(
                _finding(
                    Severity.WARNING, 32,
                    f"equation {label}: a multi-line display equation cannot be inline math; table layout used",
                )
            )
            mode = "table"
        if mode == "tabs":
            available = _available_twips(equation, plan)
            _tabs_layout(equation, number, available, layout.number_alignment == "left")
            outcomes.append(EquationOutcome(number_text, mode, section, available, None, None, True, names))
        else:
            available, side, middle = _table_layout(equation, number, plan, label, findings)
            outcomes.append(EquationOutcome(number_text, mode, section, available, side, middle, True, names))
    return outcomes, findings


__all__ = ["EquationOutcome", "MIN_MIDDLE", "MIN_SIDE", "apply_equation_layout"]
