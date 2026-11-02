"""Numbered display equation layout on hand-built WordprocessingML."""

from __future__ import annotations

from dataclasses import replace

import pytest
from lxml import etree

from latex2word.layout import ResolvedLayout
from latex2word.ooxml.equations import apply_equation_layout
from latex2word.ooxml.namespaces import NS, qn, text_of, w, xpath
from latex2word.ooxml.package import parse_xml
from latex2word.ooxml.sections import SectionPlan
from latex2word.profiles import load_profile
from latex2word.units import Length

DECLARATIONS = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NS.items() if prefix in {"w", "m", "r"})
SHORT = "<m:oMath><m:r><m:t>E=mc</m:t></m:r></m:oMath>"
WIDE = "<m:oMath>" + "".join(f"<m:r><m:t>x_{i}+</m:t></m:r>" for i in range(60)) + "</m:oMath>"
ALIGNED = (
    "<m:oMath><m:m><m:mr><m:e><m:r><m:t>a</m:t></m:r></m:e><m:e><m:r><m:t>=b</m:t></m:r></m:e></m:mr>"
    "<m:mr><m:e><m:r><m:t>c</m:t></m:r></m:e><m:e><m:r><m:t>=d</m:t></m:r></m:e></m:mr></m:m></m:oMath>"
)
TWO_LINES = SHORT + SHORT


def equation(math: str = SHORT, number: str = "(1)", bookmark: int = 1, tail: str = "") -> str:
    return (
        f'<w:p><w:pPr><w:pStyle w:val="Equation"/></w:pPr><w:bookmarkStart w:id="{bookmark}" w:name="eq{bookmark}"/>'
        f'<m:oMathPara><m:oMathParaPr><m:jc m:val="center"/></m:oMathParaPr>{math}</m:oMathPara><w:bookmarkEnd w:id="{bookmark}"/></w:p>'
        f'<w:p><w:pPr><w:pStyle w:val="EquationNumber"/></w:pPr><w:r><w:t>{number}</w:t></w:r></w:p>{tail}'
    )


def build(body: str, with_sect: bool = False) -> etree._Element:
    sect = '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>' if with_sect else ""
    return parse_xml(f"<w:document {DECLARATIONS}><w:body>{body}{sect}</w:body></w:document>".encode())


def plan_for(document: etree._Element, layout: ResolvedLayout, body_start: etree._Element | None = None) -> SectionPlan:
    return SectionPlan(layout, document.find(qn("w:body")), body_start)


def with_equations(base: ResolvedLayout, /, **changes) -> ResolvedLayout:
    return replace(base, equations=replace(base.equations, **changes))


def run_text(paragraph: etree._Element) -> str:
    """Text and tab runs of the paragraph body (the ``w:tabs`` stops of ``w:pPr`` are not content)."""
    return "".join("\t" if node.tag == w("tab") else node.text or "" for node in xpath(paragraph, "./w:r/w:tab | ./w:r/w:t"))


@pytest.fixture(scope="module")
def ieee() -> ResolvedLayout:
    return load_profile("ieee-conference").layout


@pytest.fixture(scope="module")
def generic() -> ResolvedLayout:
    return load_profile("generic").layout


def children(document: etree._Element) -> list[str]:
    return [etree.QName(c).localname for c in document.find(qn("w:body"))]


def math_para_bytes(document: etree._Element) -> list[bytes]:
    return [etree.tostring(node) for node in xpath(document, "//m:oMathPara")]


def cells(table: etree._Element) -> list[etree._Element]:
    return table.find(w("tr")).findall(w("tc"))


@pytest.mark.parametrize(("math", "number"), [(SHORT, "(1)"), (WIDE, "(12)"), (ALIGNED, "(3)")])
def test_table_mode_preserves_omml_bytes(ieee, math, number):
    document = build(equation(math, number))
    before = math_para_bytes(document)
    (outcome,), findings = apply_equation_layout(document, plan_for(document, ieee))
    assert math_para_bytes(document) == before and len(before) == 1
    assert outcome.number == number and outcome.mode == "table" and outcome.has_omml
    assert outcome.bookmark_names == ("eq1",) and outcome.section == "body"
    assert findings == []
    tables = xpath(document, "//w:tbl")
    assert len(tables) == 1 and children(document)[0] == "tbl"
    middle = cells(tables[0])[1]
    assert middle.find(qn("w:p")).find(w("bookmarkStart")).get(w("name")) == "eq1"
    assert text_of(cells(tables[0])[2]).strip() == number


def test_table_structure_and_symmetric_sides(ieee):
    document = build(equation())
    (outcome,), _ = apply_equation_layout(document, plan_for(document, ieee))
    table = xpath(document, "//w:tbl")[0]
    column, side = 5040, Length.inches("0.35").twips
    assert outcome.column_width_twips == column and outcome.side_width_twips == side
    assert outcome.middle_width_twips == column - 2 * side
    grid = [int(g.get(w("w"))) for g in table.findall(f"{w('tblGrid')}/{w('gridCol')}")]
    assert grid == [side, column - 2 * side, side] and sum(grid) == column
    props = table.find(w("tblPr"))
    assert [etree.QName(c).localname for c in props] == ["tblStyle", "tblW", "jc", "tblLayout", "tblLook"]
    assert props.find(w("tblStyle")).get(w("val")) == "EquationLayout"
    assert (props.find(w("tblW")).get(w("type")), props.find(w("tblW")).get(w("w"))) == ("dxa", "5040")
    # autofit + preferred widths: a wide equation takes width from the empty spacer instead of being clipped
    assert props.find(w("tblLayout")).get(w("type")) == "autofit"
    assert props.find(w("jc")).get(w("val")) == "center"
    assert table.find(f"{w('tr')}/{w('trPr')}/{w('cantSplit')}") is not None
    left, middle, right = cells(table)
    assert left.find(f"{w('p')}/{w('pPr')}/{w('pStyle')}").get(w("val")) == "EquationSpacer"
    assert middle.find(f"{w('p')}/{w('pPr')}/{w('pStyle')}").get(w("val")) == "Equation"
    assert right.find(f"{w('p')}/{w('pPr')}/{w('pStyle')}").get(w("val")) == "EquationNumber"
    for tc, width in zip((left, middle, right), grid, strict=True):
        assert tc.find(f"{w('tcPr')}/{w('tcW')}").get(w("w")) == str(width)
        assert tc.find(f"{w('tcPr')}/{w('vAlign')}").get(w("val")) == "center"
        expected = ["tcW", "noWrap", "vAlign"] if tc is right else ["tcW", "vAlign"]  # the number never wraps
        assert [etree.QName(c).localname for c in tc.find(w("tcPr"))] == expected
    assert not xpath(document, "//w:p[w:pPr/w:pStyle/@w:val='EquationNumber']/w:pPr/w:jc")  # alignment stays style-driven


def test_number_alignment_left_swaps_cells(ieee):
    document = build(equation(number="(7)"))
    apply_equation_layout(document, plan_for(document, with_equations(ieee, number_alignment="left")))
    left, middle, right = cells(xpath(document, "//w:tbl")[0])
    assert text_of(left).strip() == "(7)" and text_of(right).strip() == ""
    assert right.find(f"{w('p')}/{w('pPr')}/{w('pStyle')}").get(w("val")) == "EquationSpacer"
    assert left.find(f"{w('p')}/{w('pPr')}/{w('jc')}").get(w("val")) == "left"


def test_alignment_left_sets_paragraph_and_math_jc(ieee):
    document = build(equation())
    apply_equation_layout(document, plan_for(document, with_equations(ieee, alignment="left")))
    paragraph = xpath(document, "//w:p[w:pPr/w:pStyle/@w:val='Equation']")[0]
    assert paragraph.find(f"{w('pPr')}/{w('jc')}").get(w("val")) == "left"
    assert xpath(paragraph, ".//m:jc")[0].get(qn("m:val")) == "left"


def test_bookmarks_preserved_and_unique(ieee):
    body = equation(bookmark=1) + equation(SHORT, "(2)", bookmark=2) + equation(WIDE, "(3)", bookmark=3)
    document = build(body)
    before = sorted(b.get(w("name")) for b in xpath(document, "//w:bookmarkStart"))
    outcomes, _ = apply_equation_layout(document, plan_for(document, ieee))
    after = [b.get(w("name")) for b in xpath(document, "//w:bookmarkStart")]
    assert sorted(after) == before and len(set(after)) == 3
    assert [o.bookmark_names for o in outcomes] == [("eq1",), ("eq2",), ("eq3",)]
    assert len(xpath(document, "//w:bookmarkEnd")) == 3


def test_tables_are_never_adjacent(ieee):
    document = build(equation(bookmark=1) + equation(SHORT, "(2)", bookmark=2) + equation(SHORT, "(3)", bookmark=3), with_sect=True)
    apply_equation_layout(document, plan_for(document, ieee))
    assert children(document) == ["tbl", "p", "tbl", "p", "tbl", "p", "sectPr"]
    guard = document.find(qn("w:body")).findall(w("p"))[0]
    spacing = guard.find(f"{w('pPr')}/{w('spacing')}")
    assert (spacing.get(w("line")), spacing.get(w("lineRule"))) == ("20", "exact")
    assert guard.find(f"{w('pPr')}/{w('rPr')}/{w('sz')}").get(w("val")) == "2"


def test_table_before_data_table_gets_guard_and_text_after_does_not(ieee):
    data = '<w:tbl><w:tblPr><w:tblStyle w:val="Table"/></w:tblPr><w:tblGrid/><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl>'
    document = build(equation(tail=data))
    apply_equation_layout(document, plan_for(document, ieee))
    assert children(document) == ["tbl", "p", "tbl"]
    document = build(equation(tail='<w:p><w:r><w:t>text</w:t></w:r></w:p>'), with_sect=True)
    apply_equation_layout(document, plan_for(document, ieee))
    assert children(document) == ["tbl", "p", "sectPr"]


def test_pass_is_idempotent(ieee):
    document = build(equation())
    apply_equation_layout(document, plan_for(document, ieee))
    snapshot = etree.tostring(document)
    outcomes, findings = apply_equation_layout(document, plan_for(document, ieee))
    assert outcomes == [] and findings == [] and etree.tostring(document) == snapshot


def test_unpaired_equation_untouched(ieee):
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Equation"/></w:pPr><m:oMathPara>' + SHORT + "</m:oMathPara></w:p>"
        '<w:p><w:r><w:t>text follows</w:t></w:r></w:p>'
    )
    document = build(body)
    snapshot = etree.tostring(document)
    outcomes, findings = apply_equation_layout(document, plan_for(document, ieee))
    assert outcomes == [] and findings == [] and etree.tostring(document) == snapshot


def test_narrow_column_warns_and_reduces_sides_symmetrically(ieee):
    layout = with_equations(ieee, number_width=Length.inches("1.4"))
    document = build(equation())
    (outcome,), findings = apply_equation_layout(document, plan_for(document, layout))
    assert [f.code for f in findings] == ["L2W-W-LAYOUT-031"]
    assert outcome.side_width_twips == (5040 - 1440) // 2 == 1800
    assert outcome.middle_width_twips == 1440
    grid = [int(g.get(w("w"))) for g in xpath(document, "//w:gridCol")]
    assert grid[0] == grid[2] and sum(grid) == 5040


def test_side_floor_is_a_quarter_inch(ieee):
    cell = (
        '<w:tbl><w:tblPr><w:tblStyle w:val="Table"/></w:tblPr><w:tblGrid><w:gridCol w:w="1500"/></w:tblGrid>'
        f'<w:tr><w:tc><w:tcPr><w:tcW w:w="1500" w:type="dxa"/></w:tcPr>{equation()}</w:tc></w:tr></w:tbl>'
    )
    document = build(cell)
    (outcome,), findings = apply_equation_layout(document, plan_for(document, ieee))
    assert outcome.side_width_twips == Length.inches("0.25").twips == 360
    assert outcome.middle_width_twips == 1500 - 720
    assert findings[0].code == "L2W-W-LAYOUT-031"


def test_section_aware_column_width(ieee):
    body = equation(bookmark=1) + '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr></w:p>' + equation(SHORT, "(2)", bookmark=2)
    document = build(body)
    start = document.find(qn("w:body")).findall(w("p"))[2]  # the Heading1
    outcomes, _ = apply_equation_layout(document, plan_for(document, ieee, start))
    assert [o.section for o in outcomes] == ["frontmatter", "body"]
    assert [o.column_width_twips for o in outcomes] == [10440, 5040]


def test_single_column_generic_layout(generic):
    document = build(equation())
    (outcome,), _ = apply_equation_layout(document, plan_for(document, generic))
    assert outcome.column_width_twips == Length.inches("6.5").twips == 9360
    assert outcome.side_width_twips == Length.inches("0.5").twips


def test_tabs_mode(ieee):
    layout = with_equations(ieee, mode="tabs")
    document = build(equation(number="(12)"))
    math_before = [etree.tostring(m) for m in xpath(document, "//m:oMath")]
    (outcome,), findings = apply_equation_layout(document, plan_for(document, layout))
    assert findings == [] and outcome.mode == "tabs" and outcome.side_width_twips is None and outcome.number == "(12)"
    assert children(document) == ["p"] and not xpath(document, "//w:tbl | //m:oMathPara")
    paragraph = document.find(qn("w:body")).find(w("p"))
    names = [etree.QName(c).localname for c in paragraph]
    assert names == ["pPr", "bookmarkStart", "r", "oMath", "r", "bookmarkEnd", "r"]
    assert [etree.tostring(m) for m in xpath(document, "//m:oMath")] == math_before
    stops = [(t.get(w("val")), int(t.get(w("pos")))) for t in xpath(paragraph, "w:pPr/w:tabs/w:tab")]
    assert stops == [("center", 2520), ("right", 5040)]
    assert [etree.QName(c).localname for c in paragraph.find(w("pPr"))] == ["pStyle", "tabs", "jc"]
    assert run_text(paragraph) == "\t\t(12)"
    assert len(xpath(paragraph, "./w:bookmarkStart")) == 1 and len(xpath(paragraph, "./w:bookmarkEnd")) == 1


def test_tabs_mode_number_left(ieee):
    layout = with_equations(ieee, mode="tabs", number_alignment="left")
    document = build(equation(number="(4)"))
    apply_equation_layout(document, plan_for(document, layout))
    paragraph = document.find(qn("w:body")).find(w("p"))
    assert run_text(paragraph) == "(4)\t"
    assert [(t.get(w("val"))) for t in xpath(paragraph, "w:pPr/w:tabs/w:tab")] == ["center"]


def test_tabs_mode_multiline_falls_back_to_table(ieee):
    layout = with_equations(ieee, mode="tabs")
    document = build(equation(TWO_LINES))
    (outcome,), findings = apply_equation_layout(document, plan_for(document, layout))
    assert outcome.mode == "table" and [f.code for f in findings] == ["L2W-W-LAYOUT-032"]
    assert len(xpath(document, "//w:tbl")) == 1 and len(xpath(document, "//m:oMathPara/m:oMath")) == 2


def test_equation_in_table_cell_uses_cell_width(ieee):
    cell = (
        '<w:tbl><w:tblPr><w:tblStyle w:val="Table"/></w:tblPr><w:tblGrid><w:gridCol w:w="3000"/></w:tblGrid>'
        f'<w:tr><w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr>{equation()}</w:tc></w:tr></w:tbl>'
    )
    document = build(cell)
    (outcome,), _ = apply_equation_layout(document, plan_for(document, ieee))
    assert outcome.column_width_twips == 3000
    outer = document.find(qn("w:body")).find(w("tbl"))
    last = outer.find(f"{w('tr')}/{w('tc')}")[-1]
    assert last.tag == w("p")  # a cell must end with a paragraph, so the table got a guard behind it
