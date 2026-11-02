"""Table fitting on hand-built WordprocessingML."""

from __future__ import annotations

from dataclasses import replace

import pytest
from lxml import etree

from latex2word.layout import ResolvedLayout
from latex2word.ooxml.namespaces import NS, qn, w, xpath
from latex2word.ooxml.package import parse_xml
from latex2word.ooxml.sections import SectionPlan
from latex2word.ooxml.tables import apply_table_layout, column_widths, distribute
from latex2word.profiles import load_profile
from latex2word.units import Length

DECLARATIONS = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NS.items() if prefix in {"w", "m"})


def cell(text: str, span: int = 1, math: bool = False) -> str:
    properties = f'<w:tcPr><w:gridSpan w:val="{span}"/></w:tcPr>' if span > 1 else ""
    inner = f"<m:oMath><m:r><m:t>{text}</m:t></m:r></m:oMath>" if math else f"<w:r><w:t>{text}</w:t></w:r>"
    return f'<w:tc>{properties}<w:p><w:pPr><w:pStyle w:val="Compact"/><w:jc w:val="left"/></w:pPr>{inner}</w:p></w:tc>'


def table(rows: list[list[str]], grid: list[int] | None = None, style: str = "Table", header: bool = True) -> str:
    columns = len(rows[0])
    grid = grid if grid is not None else [0] * columns
    cols = "".join(f'<w:gridCol w:w="{width}"/>' for width in grid)
    body = ""
    for index, row in enumerate(rows):
        trpr = "<w:trPr><w:tblHeader/></w:trPr>" if header and index == 0 else ""
        body += f"<w:tr>{trpr}{''.join(c if c.startswith('<w:tc>') else cell(c) for c in row)}</w:tr>"
    return (
        f'<w:tbl><w:tblPr><w:tblStyle w:val="{style}"/><w:tblW w:type="auto" w:w="0"/>'
        f'<w:tblLook w:val="0020" w:firstRow="1"/><w:tblCaption w:val="t"/></w:tblPr><w:tblGrid>{cols}</w:tblGrid>{body}</w:tbl>'
    )


def build(body: str) -> etree._Element:
    return parse_xml(f"<w:document {DECLARATIONS}><w:body>{body}</w:body></w:document>".encode())


def plan_for(document: etree._Element, layout: ResolvedLayout, body_start: etree._Element | None = None) -> SectionPlan:
    return SectionPlan(layout, document.find(qn("w:body")), body_start)


def with_tables(base: ResolvedLayout, /, **changes) -> ResolvedLayout:
    return replace(base, tables=replace(base.tables, **changes))


@pytest.fixture(scope="module")
def ieee() -> ResolvedLayout:
    return load_profile("ieee-conference").layout


@pytest.fixture(scope="module")
def generic() -> ResolvedLayout:
    return load_profile("generic").layout


def grid_of(tbl: etree._Element) -> list[int]:
    return [int(c.get(w("w"))) for c in tbl.findall(f"{w('tblGrid')}/{w('gridCol')}")]


def tblpr_children(tbl: etree._Element) -> list[str]:
    return [etree.QName(c).localname for c in tbl.find(w("tblPr"))]


SAMPLE = [["Name", "Value", "Notes"], ["alpha", "1", "a short note"], ["beta", "2", "a considerably longer explanatory note"]]
WIDTH_COLUMN = 5040  # 3.5in IEEE body column


def test_distribute_sums_exactly():
    for total in (1, 5040, 9999):
        assert sum(distribute(total, [1, 1, 1])) == total
    assert distribute(10, [1, 1, 1]) == [4, 3, 3]
    assert sum(column_widths(5040, [1.0, 30.0, 4.0, 2.0], 360)) == 5040
    assert min(column_widths(5040, [1.0, 30.0, 4.0, 2.0], 360)) >= 360


def test_table_fits_column_exactly(ieee):
    document = build(table(SAMPLE))
    (outcome,), findings = apply_table_layout(document, plan_for(document, ieee))
    tbl = document.find(f".//{w('tbl')}")
    tbl_w = tbl.find(f"{w('tblPr')}/{w('tblW')}")
    assert (tbl_w.get(w("type")), int(tbl_w.get(w("w")))) == ("dxa", WIDTH_COLUMN)
    assert sum(grid_of(tbl)) == WIDTH_COLUMN == outcome.width_twips == outcome.available_twips
    assert outcome.fits and outcome.layout == "fixed" and outcome.section == "body"
    assert (outcome.columns, outcome.rows, outcome.caption) == (3, 3, None)
    assert findings == []
    # the longer-text column is the widest, none below 0.25in
    widths = grid_of(tbl)
    assert widths[2] == max(widths) and min(widths) >= Length.inches("0.25").twips


def test_tblpr_schema_order_and_properties(ieee):
    document = build(table(SAMPLE))
    apply_table_layout(document, plan_for(document, ieee))
    tbl = document.find(f".//{w('tbl')}")
    assert tblpr_children(tbl) == ["tblStyle", "tblW", "jc", "tblLayout", "tblLook", "tblCaption"]
    assert tbl.find(f"{w('tblPr')}/{w('jc')}").get(w("val")) == "center"
    assert tbl.find(f"{w('tblPr')}/{w('tblLayout')}").get(w("type")) == "fixed"
    assert tbl.find(f"{w('tblPr')}/{w('tblCellMar')}") is None


def test_cell_widths_follow_grid_and_spans(ieee):
    rows = [["a", "b", "c"], [cell("wide span", 2), "x"]]
    document = build(table(rows))
    apply_table_layout(document, plan_for(document, ieee))
    tbl = document.find(f".//{w('tbl')}")
    grid = grid_of(tbl)
    first, second = tbl.findall(w("tr"))
    assert [int(tc.find(f"{w('tcPr')}/{w('tcW')}").get(w("w"))) for tc in first.findall(w("tc"))] == grid
    assert [int(tc.find(f"{w('tcPr')}/{w('tcW')}").get(w("w"))) for tc in second.findall(w("tc"))] == [grid[0] + grid[1], grid[2]]
    span = second.find(w("tc")).find(w("tcPr"))
    assert [etree.QName(c).localname for c in span] == ["tcW", "gridSpan"]


def test_real_table_preserved(ieee):
    rows = [["h1", "h2"], ["plain", cell("x+y", math=True)]]
    document = build(table(rows))
    before_text = [("".join(p.itertext())) for p in xpath(document, "//w:p")]
    before_math = [etree.tostring(m) for m in xpath(document, "//m:oMath")]
    apply_table_layout(document, plan_for(document, ieee))
    assert ["".join(p.itertext()) for p in xpath(document, "//w:p")] == before_text
    assert [etree.tostring(m) for m in xpath(document, "//m:oMath")] == before_math
    assert len(xpath(document, "//w:tbl")) == 1 and len(xpath(document, "//w:tr")) == 2
    # direct cell alignment from Pandoc is not rewritten
    assert all(jc.get(w("val")) == "left" for jc in xpath(document, "//w:pPr/w:jc"))


def test_keep_rows_flags(ieee):
    document = build(table(SAMPLE))
    apply_table_layout(document, plan_for(document, ieee))
    rows = xpath(document, "//w:tr")
    for row in rows:
        assert row.find(f"{w('trPr')}/{w('cantSplit')}") is not None
    assert [etree.QName(c).localname for c in rows[0].find(w("trPr"))] == ["cantSplit", "tblHeader"]
    keep = [[p.find(f"{w('pPr')}/{w('keepNext')}") is not None for p in row.iter(w("p"))] for row in rows]
    assert all(keep[0]) and all(keep[1]) and not any(keep[2])
    order = [etree.QName(c).localname for c in rows[0].find(f"{w('tc')}/{w('p')}/{w('pPr')}")]
    assert order == ["pStyle", "keepNext", "jc"]


def test_keep_rows_disabled(ieee):
    document = build(table(SAMPLE))
    apply_table_layout(document, plan_for(document, with_tables(ieee, keep_rows=False)))
    assert not xpath(document, "//w:cantSplit | //w:keepNext")
    assert xpath(document, "//w:tblHeader")


def test_alignment_and_autofit_layout(ieee):
    layout = with_tables(ieee, alignment="right", width=Length.inches("2"), layout="autofit")
    document = build(table(SAMPLE))
    (outcome,), findings = apply_table_layout(document, plan_for(document, layout))
    tbl = document.find(f".//{w('tbl')}")
    assert tbl.find(f"{w('tblPr')}/{w('jc')}").get(w("val")) == "right"
    assert tbl.find(f"{w('tblPr')}/{w('tblLayout')}").get(w("type")) == "autofit"
    assert sum(grid_of(tbl)) == 2880 == outcome.width_twips and outcome.layout == "autofit"
    assert findings == []


def test_column_width_forces_fixed_layout(ieee):
    layout = with_tables(ieee, layout="autofit")
    document = build(table(SAMPLE))
    (outcome,), _ = apply_table_layout(document, plan_for(document, layout))
    assert outcome.layout == "fixed"


def test_length_wider_than_column_is_clamped(ieee):
    layout = with_tables(ieee, width=Length.inches("5"))
    document = build(table(SAMPLE))
    (outcome,), findings = apply_table_layout(document, plan_for(document, layout))
    assert outcome.width_twips == WIDTH_COLUMN
    assert "L2W-W-LAYOUT-021" in [f.code for f in findings]


def test_auto_width_keeps_narrow_pandoc_grid(ieee):
    layout = with_tables(ieee, width="auto")
    document = build(table(SAMPLE, grid=[1000, 1000, 2000]))
    (outcome,), findings = apply_table_layout(document, plan_for(document, layout))
    tbl = document.find(f".//{w('tbl')}")
    assert grid_of(tbl) == [1000, 1000, 2000] and outcome.width_twips == 4000
    assert tbl.find(f"{w('tblPr')}/{w('tblW')}").get(w("type")) == "auto"
    assert not xpath(tbl, ".//w:tcW") and findings == []


def test_auto_width_wide_pandoc_grid_is_scaled_down(ieee):
    layout = with_tables(ieee, width="auto")
    document = build(table(SAMPLE, grid=[4000, 4000, 8000]))
    (outcome,), findings = apply_table_layout(document, plan_for(document, layout))
    tbl = document.find(f".//{w('tbl')}")
    widths = grid_of(tbl)
    assert sum(widths) == WIDTH_COLUMN == outcome.width_twips and outcome.fits
    assert widths[2] == pytest.approx(2 * widths[0], abs=2)
    assert [f.code for f in findings] == ["L2W-I-LAYOUT-024"]
    assert tbl.find(f"{w('tblPr')}/{w('tblW')}").get(w("type")) == "auto"
    assert sum(int(c.get(w("w"))) for c in tbl.find(w("tr")).iterfind(f"{w('tc')}/{w('tcPr')}/{w('tcW')}")) == WIDTH_COLUMN


def test_too_many_columns_is_a_hard_fit_error(ieee):
    rows = [[f"c{i}" for i in range(16)], [str(i) for i in range(16)]]
    document = build(table(rows))
    (outcome,), findings = apply_table_layout(document, plan_for(document, ieee))
    codes = [f.code for f in findings]
    assert "L2W-E-LAYOUT-023" in codes
    assert sum(grid_of(document.find(f".//{w('tbl')}"))) == WIDTH_COLUMN
    assert not outcome.fits


def test_unfittable_words_warn_and_never_shrink_fonts(ieee):
    long = "Supercalifragilisticexpialidocious" * 2
    document = build(table([[long, long, long], ["a", "b", "c"]]))
    (outcome,), findings = apply_table_layout(document, plan_for(document, ieee))
    warning = next(f for f in findings if f.code == "L2W-W-LAYOUT-020")
    assert "shrinking fonts is not allowed" in warning.message
    assert outcome.min_content_estimate_twips > outcome.available_twips and not outcome.fits
    assert not xpath(document, "//w:sz | //w:rFonts | //w:szCs")


def test_font_floor_warning(ieee):
    document = build(table(SAMPLE))
    _, findings = apply_table_layout(document, plan_for(document, with_tables(ieee, min_font_pt=9.0)))
    assert [f.code for f in findings] == ["L2W-W-LAYOUT-022"]
    document = build(table(SAMPLE))
    _, findings = apply_table_layout(document, plan_for(document, with_tables(ieee, min_font_pt=8.0)))
    assert findings == []


def test_layout_tables_are_skipped(ieee):
    body = table(SAMPLE, style="EquationLayout") + "<w:p/>" + table(SAMPLE, style="AuthorBlock") + "<w:p/>" + table(SAMPLE)
    document = build(body)
    before = [etree.tostring(t) for t in xpath(document, "//w:tbl")[:2]]
    outcomes, _ = apply_table_layout(document, plan_for(document, ieee))
    assert len(outcomes) == 1 and outcomes[0].index == 1
    assert [etree.tostring(t) for t in xpath(document, "//w:tbl")[:2]] == before


def test_section_aware_width(ieee):
    body = table(SAMPLE) + '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr></w:p>' + table(SAMPLE)
    document = build(body)
    start = document.find(qn("w:body")).findall(w("p"))[0]
    outcomes, _ = apply_table_layout(document, plan_for(document, ieee, start))
    assert [o.section for o in outcomes] == ["frontmatter", "body"]
    assert outcomes[0].width_twips == ieee.frontmatter_section.column_width.twips == 10440
    assert outcomes[1].width_twips == WIDTH_COLUMN


def test_caption_text_is_reported(ieee):
    caption = '<w:p><w:pPr><w:pStyle w:val="TableCaption"/></w:pPr><w:r><w:t>TABLE I</w:t></w:r></w:p>'
    document = build(caption + table(SAMPLE))
    (outcome,), _ = apply_table_layout(document, plan_for(document, ieee))
    assert outcome.caption == "TABLE I"
    assert outcome.to_dict()["width_twips"] == WIDTH_COLUMN


def test_generic_single_column(generic):
    document = build(table(SAMPLE))
    (outcome,), _ = apply_table_layout(document, plan_for(document, generic))
    assert outcome.available_twips == Length.inches("6.5").twips
