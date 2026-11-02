"""Profile-driven layout of the data tables of a Pandoc DOCX (publisher-neutral).

Real Word tables stay real tables: only ``w:tblPr``, ``w:tblGrid``, ``w:tcW`` and
the row/paragraph keep flags are written, cell content is never touched. Fonts are
never shrunk to make a table fit; a table that cannot fit its column is reported
(``L2W-W-LAYOUT-020``) rather than silently rescaled.

Layout tables created elsewhere (equation layout, author block) are skipped.
Nested tables are skipped too: their available width is the enclosing cell, which
this pass does not model.

Column widths are proportional to a *content weight* (half the longest word plus
half the mean text length of the column, a cheap stand-in for text measurement
that needs no fonts), never narrower than :data:`MIN_COLUMN` and always summing
to the table width exactly: the integer twips are distributed by largest
remainder, because Word lays out from the grid and a one-twip drift shows up as a
ragged right edge.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from lxml import etree

from latex2word.diagnostics import Severity
from latex2word.layout import ResolvedLayout, TextStyle
from latex2word.ooxml import style_ids
from latex2word.ooxml.namespaces import qn, text_of, w, xpath
from latex2word.ooxml.sections import SectionPlan
from latex2word.ooxml.xmlutil import (
    PPR_ORDER,
    TBLPR_ORDER,
    TCPR_ORDER,
    TRPR_ORDER,
    ensure_child,
    paragraph_style,
    ppr,
    set_child,
    set_paragraph_style,
)
from latex2word.source_graph import Finding
from latex2word.units import Length

MIN_COLUMN = Length.inches("0.25")
_EM_PER_CHAR = 0.5  # average glyph advance as a fraction of the font size
_DEFAULT_FONT_PT = 10.0


@dataclass(frozen=True)
class TableOutcome:
    """The layout decided for one data table."""

    index: int
    caption: str | None
    columns: int
    rows: int
    width_twips: int | None  # None: auto width with an unmeasured (all-zero) Pandoc grid
    available_twips: int
    fits: bool
    layout: str  # fixed | autofit
    section: str  # frontmatter | body
    min_content_estimate_twips: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "caption": self.caption, "columns": self.columns, "rows": self.rows,
            "width_twips": self.width_twips, "available_twips": self.available_twips, "fits": self.fits,
            "layout": self.layout, "section": self.section,
            "min_content_estimate_twips": self.min_content_estimate_twips,
        }


@dataclass
class _Cell:
    element: etree._Element
    start: int  # first grid column
    span: int
    text: str


def _finding(severity: Severity, number: int, message: str) -> Finding:
    return Finding(f"L2W-{severity.value[0].upper()}-LAYOUT-{number:03d}", severity, message)


# --------------------------------------------------------------------------- pure arithmetic


def distribute(total: int, shares: Sequence[float]) -> list[int]:
    """Integers proportional to ``shares`` that sum to exactly ``total`` (largest remainder)."""
    scale = sum(shares)
    if not shares:
        return []
    exact = [total * share / scale for share in shares] if scale > 0 else [total / len(shares)] * len(shares)
    floors = [int(value) for value in exact]
    order = sorted(range(len(exact)), key=lambda i: (floors[i] - exact[i], i))
    for i in order[: total - sum(floors)]:
        floors[i] += 1
    return floors


def column_widths(total: int, weights: Sequence[float], minimum: int) -> list[int]:
    """Widths proportional to ``weights``, none below ``minimum``, summing exactly to ``total``."""
    count = len(weights)
    if count * minimum >= total:
        return distribute(total, [1.0] * count)
    pinned: set[int] = set()
    while True:
        free = [i for i in range(count) if i not in pinned]
        pool = total - minimum * len(pinned)
        weight_sum = sum(weights[i] for i in free)
        shares = {i: pool * weights[i] / weight_sum for i in free}
        low = {i for i, share in shares.items() if share < minimum}
        if not low:
            break
        pinned |= low
    return distribute(total, [float(minimum) if i in pinned else shares[i] for i in range(count)])


# --------------------------------------------------------------------------- table reading


def _is_data_table(table: etree._Element) -> bool:
    style = table.find(f"{w('tblPr')}/{w('tblStyle')}")
    if style is not None and style.get(w("val")) in style_ids.LAYOUT_TABLE_STYLES:
        return False
    return not any(ancestor.tag == w("tc") for ancestor in table.iterancestors())


def _int_attr(element: etree._Element | None, name: str, default: int = 0) -> int:
    try:
        return int(element.get(w(name), default)) if element is not None else default
    except ValueError:
        return default


def _cell_text(cell: etree._Element) -> str:
    paragraphs = cell.findall(w("p"))
    return " ".join(
        text_of(p) + "".join(node.text or "" for node in xpath(p, ".//m:t")) for p in paragraphs
    ).strip()


def _read_rows(table: etree._Element) -> list[list[_Cell]]:
    rows: list[list[_Cell]] = []
    for tr in table.findall(w("tr")):
        position = _int_attr(tr.find(f"{w('trPr')}/{w('gridBefore')}"), "val")
        cells: list[_Cell] = []
        for tc in tr.findall(w("tc")):
            span = max(1, _int_attr(tc.find(f"{w('tcPr')}/{w('gridSpan')}"), "val", 1))
            cells.append(_Cell(tc, position, span, _cell_text(tc)))
            position += span
        rows.append(cells)
    return rows


def _grid(table: etree._Element, rows: list[list[_Cell]]) -> list[int]:
    cols = [_int_attr(col, "w") for col in table.findall(f"{w('tblGrid')}/{w('gridCol')}")]
    if cols:
        return cols
    return [0] * max((cell.start + cell.span for row in rows for cell in row), default=0)


def _weights(rows: list[list[_Cell]], count: int) -> tuple[list[float], list[int]]:
    """Per-column content weight and longest-word length (characters) over single-column cells."""
    longest = [1] * count
    lengths: list[list[int]] = [[] for _ in range(count)]
    for row in rows:
        for cell in row:
            if cell.span != 1 or cell.start >= count:
                continue
            words = cell.text.split()
            longest[cell.start] = max(longest[cell.start], max(map(len, words), default=1))
            lengths[cell.start].append(len(cell.text))
    means = [sum(values) / len(values) if values else 1.0 for values in lengths]
    return [0.5 * word + 0.5 * mean for word, mean in zip(longest, means, strict=True)], longest


def _caption(table: etree._Element) -> str | None:
    for sibling in (_sibling(table, previous=True), _sibling(table, previous=False)):
        if sibling is not None and sibling.tag == w("p") and paragraph_style(sibling) == style_ids.TABLE_CAPTION:
            return text_of(sibling).strip()
    return None


def _sibling(element: etree._Element, *, previous: bool) -> etree._Element | None:
    node = element.getprevious() if previous else element.getnext()
    while node is not None and node.tag in (w("bookmarkStart"), w("bookmarkEnd")):
        node = node.getprevious() if previous else node.getnext()
    return node


# --------------------------------------------------------------------------- writing


def _table_font_pt(layout: ResolvedLayout) -> float | None:
    for role in ("table_text", "default"):
        style = layout.typography.get(role)
        if style is not None and style.size_pt is not None:
            return style.size_pt
    return None


def _set_grid(table: etree._Element, widths: Sequence[int]) -> None:
    grid = ensure_child(table, "tblGrid", ("tblPr", "tblGrid", "tr"))
    for child in list(grid):
        grid.remove(child)
    for width in widths:
        etree.SubElement(grid, w("gridCol")).set(w("w"), str(width))


def _set_cell_widths(rows: list[list[_Cell]], widths: Sequence[int]) -> None:
    for row in rows:
        for cell in row:
            tc_pr = ensure_child(cell.element, "tcPr", ("tcPr", "p", "tbl"))
            total = sum(widths[cell.start : cell.start + cell.span])
            set_child(tc_pr, "tcW", TCPR_ORDER, w=str(total), type="dxa")


def _keep_rows(table: etree._Element) -> None:
    """``cantSplit`` on every row; ``keepNext`` in all but the last row so Word keeps the table in one piece."""
    table_rows = table.findall(w("tr"))
    for index, tr in enumerate(table_rows):
        set_child(ensure_child(tr, "trPr", ("tblPrEx", "trPr", "tc")), "cantSplit", TRPR_ORDER)
        if index == len(table_rows) - 1:
            continue
        for tc in tr.findall(w("tc")):
            for paragraph in tc.findall(w("p")):
                set_child(ppr(paragraph), "keepNext", PPR_ORDER)


def _keep_header(table: etree._Element) -> None:
    """``cantSplit`` on every row; ``keepNext`` only in the header rows (and caption side) and the first data row."""
    table_rows = table.findall(w("tr"))
    header = sum(1 for tr in table_rows if tr.find(f"{w('trPr')}/{w('tblHeader')}") is not None)
    for index, tr in enumerate(table_rows):
        set_child(ensure_child(tr, "trPr", ("tblPrEx", "trPr", "tc")), "cantSplit", TRPR_ORDER)
        if index >= header + 1 or index == len(table_rows) - 1:
            continue
        for tc in tr.findall(w("tc")):
            for paragraph in tc.findall(w("p")):
                set_child(ppr(paragraph), "keepNext", PPR_ORDER)


def _row_style_text(layout: ResolvedLayout, table: etree._Element) -> TextStyle | None:
    uncaptioned = any(paragraph_style(p) == style_ids.COMPACT_UNCAPTIONED for p in table.iter(w("p")))
    role = "table_text_uncaptioned" if uncaptioned else "table_text"
    return layout.typography.get(role) or layout.typography.get("table_text")


def _estimated_height(
    rows: list[list[_Cell]], widths: Sequence[int], available: int, layout: ResolvedLayout, table: etree._Element
) -> int:
    """Rough height in twips: per row, the most wrapped cell (characters x average width / cell width) x line height."""
    style = _row_style_text(layout, table)
    font_pt = (style.size_pt if style is not None and style.size_pt else None) or _table_font_pt(layout) or _DEFAULT_FONT_PT
    if style is not None and style.line_height is not None:
        line = style.line_height.twips
    else:
        line = round(font_pt * 1.2 * 20 * (style.line_spacing if style is not None and style.line_spacing else 1.0))
    per_char = font_pt * 20 * _EM_PER_CHAR
    margin = _cell_margin_twips(layout)
    count = max(1, len(widths))
    total = 0
    for row in rows:
        lines = 1
        for cell in row:
            cell_width = sum(widths[cell.start : cell.start + cell.span]) if widths else available * cell.span // count
            usable = max(cell_width - margin, 200)
            lines = max(lines, -(-round(len(cell.text) * per_char) // usable))
        total += lines * line
    return total


def _target_width(layout: ResolvedLayout, available: int) -> tuple[int | None, bool]:
    """``(fixed table width in twips | None for auto, clamped?)`` for ``tables.width``."""
    setting = layout.tables.width
    if setting == "auto":
        return None, False
    if setting == "column":
        return available, False
    requested = setting.twips  # type: ignore[union-attr]
    return min(requested, available), requested > available


def _cell_margin_twips(layout: ResolvedLayout) -> int:
    return layout.tables.margin_left.twips + layout.tables.margin_right.twips


def _layout_table(
    table: etree._Element, index: int, plan: SectionPlan, findings: list[Finding]
) -> TableOutcome:
    layout = plan.layout
    available = plan.geometry_for(table).column_width.twips
    section = "frontmatter" if plan.is_frontmatter(table) else "body"
    rows = _read_rows(table)
    grid = _grid(table, rows)
    count = len(grid)
    label = f"table {index}"
    weights, longest = _weights(rows, count)
    font_pt = _table_font_pt(layout)

    minimum_floor = layout.tables.min_font_pt
    if minimum_floor is not None and font_pt is not None and font_pt < minimum_floor:
        findings.append(
            _finding(
                Severity.WARNING, 22,
                f"{label}: table font {font_pt:g}pt is below the profile minimum {minimum_floor:g}pt; fonts are never changed here",
            )
        )

    per_char = (font_pt or _DEFAULT_FONT_PT) * 20 * _EM_PER_CHAR
    estimate = round(sum(chars * per_char + _cell_margin_twips(layout) for chars in longest))
    fits = estimate <= available
    if not fits:
        findings.append(
            _finding(
                Severity.WARNING, 20,
                f"{label}: minimal content width {estimate} twips exceeds the column width {available} twips; "
                "the table may not fit the column; shrinking fonts is not allowed",
            )
        )

    target, clamped = _target_width(layout, available)
    if clamped:
        findings.append(
            _finding(
                Severity.WARNING, 21,
                f"{label}: configured width {layout.tables.width} exceeds the column width {available} twips; clamped",
            )
        )
    grid_sum = sum(grid)
    minimum = MIN_COLUMN.twips
    if target is None and grid_sum > available:
        target_grid, scaled = available, True
    else:
        target_grid, scaled = target, False
    if target_grid is None and grid_sum == 0 and layout.tables.layout == "fixed":
        target_grid = available  # a fixed layout needs a measured grid; an auto-width table keeps its auto tblW

    widths = grid
    if target_grid is not None:
        if count and count * minimum > target_grid:
            findings.append(
                _finding(
                    Severity.ERROR, 23,
                    f"{label}: {count} columns cannot each keep the minimum width of {minimum} twips within "
                    f"{target_grid} twips; the widths are not usable as laid out",
                )
            )
            fits = False
        widths = column_widths(target_grid, weights, minimum) if not scaled else distribute(target_grid, grid)
        if scaled and count * minimum <= target_grid:
            findings.append(
                _finding(
                    Severity.INFO, 24,
                    f"{label}: Pandoc grid ({grid_sum} twips) scaled down to the column width {available} twips",
                )
            )
        _set_grid(table, widths)

    table_pr = ensure_child(table, "tblPr", ("tblPr", "tblGrid", "tr"))
    set_child(table_pr, "jc", TBLPR_ORDER, val=layout.tables.alignment)
    if target is not None:
        set_child(table_pr, "tblW", TBLPR_ORDER, w=str(target), type="dxa")
    fixed = layout.tables.layout == "fixed" or layout.tables.width == "column"
    set_child(table_pr, "tblLayout", TBLPR_ORDER, type="fixed" if fixed else "autofit")
    if target is not None or scaled or any(
        cell.element.find(f"{w('tcPr')}/{w('tcW')}") is not None for row in rows for cell in row
    ):
        _set_cell_widths(rows, widths)
    if layout.tables.keep_rows:
        # a table taller than the page cannot be kept in one piece: Word would only move it and leave blank
        # pages (LaTeX's longtable breaks); such a table keeps unsplittable rows and its header with the first row
        if _estimated_height(rows, widths or grid, available, layout, table) <= layout.page.text_height.twips:
            _keep_rows(table)
        else:
            _keep_header(table)

    final = sum(widths)
    return TableOutcome(
        index, _caption(table), count, len(rows), final if final > 0 else None, available,
        fits and final <= available, "fixed" if fixed else "autofit", section, estimate,
    )


def apply_table_layout(
    document: etree._Element, plan: SectionPlan, layout: ResolvedLayout | None = None
) -> tuple[list[TableOutcome], list[Finding]]:
    """Fit every data table to its section's column (see module docstring); ``layout`` defaults to ``plan.layout``."""
    if layout is not None and layout is not plan.layout:
        plan = SectionPlan(layout, plan.body, plan.body_start)
    findings: list[Finding] = []
    outcomes: list[TableOutcome] = []
    for table in [t for t in document.iter(qn("w:tbl")) if _is_data_table(t)]:
        outcomes.append(_layout_table(table, len(outcomes) + 1, plan, findings))
    return outcomes, findings


def restyle_uncaptioned_tables(document: etree._Element, layout: ResolvedLayout) -> int:
    """Cells of data tables without a caption take the ``table_text_uncaptioned`` role (when the profile sets it).

    A captioned table is a float or a numbered long table; an uncaptioned one is running content such as a
    list of abbreviations and usually keeps the body size. Returns the number of tables restyled.
    """
    if "table_text_uncaptioned" not in layout.typography:
        return 0
    count = 0
    for table in [t for t in document.iter(qn("w:tbl")) if _is_data_table(t)]:
        if _caption(table) is not None or table.find(f"{w('tblPr')}/{w('tblCaption')}") is not None:
            continue
        if any(a.tag == qn("w:tbl") for a in table.iterancestors()):
            continue  # a table nested in another table follows its outer table
        changed = False
        for paragraph in table.iter(w("p")):
            if paragraph_style(paragraph) in ("", style_ids.COMPACT):
                set_paragraph_style(paragraph, style_ids.COMPACT_UNCAPTIONED)
                changed = True
        count += int(changed)
    return count


def restyle_image_grids(document: etree._Element) -> int:
    """Uncaptioned tables holding only images (no text, no math) become borderless layout grids.

    Such a table is how LaTeX places images side by side (a ``tabular`` of ``includegraphics`` cells without
    rules); the data-table rules would draw lines that the source never had.
    Returns the number of tables restyled.
    """
    count = 0
    for table in [t for t in document.iter(qn("w:tbl")) if _is_data_table(t)]:
        if _caption(table) is not None or table.find(f"{w('tblPr')}/{w('tblCaption')}") is not None:
            continue
        if table.find(f".//{w('drawing')}") is None or table.find(f".//{w('tbl')}") is not None:
            continue
        if any((t.text or "").strip() for t in table.iter(w("t"))) or table.find(".//{*}oMath") is not None:
            continue
        set_child(ensure_child(table, "tblPr", ("tblPr", "tblGrid", "tr")), "tblStyle", TBLPR_ORDER,
                  val=style_ids.TABLE_IMAGE_LAYOUT)
        count += 1
    return count


__all__ = [
    "MIN_COLUMN", "TableOutcome", "apply_table_layout", "column_widths", "distribute", "restyle_image_grids",
    "restyle_uncaptioned_tables",
]
