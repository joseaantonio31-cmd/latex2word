"""Figure extents and caption placement on hand-built WordprocessingML."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from lxml import etree

from latex2word.layout import ResolvedLayout
from latex2word.ooxml.drawings import FigureSpec, apply_caption_placement, apply_figure_extents
from latex2word.ooxml.namespaces import NS, qn, w, xpath
from latex2word.ooxml.package import OpcPackage, parse_xml
from latex2word.ooxml.sections import SectionPlan
from latex2word.profiles import load_profile
from latex2word.units import EMU_PER_INCH, Length

DECLARATIONS = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NS.items() if prefix in {"w", "m", "r", "wp", "a", "pic", "asvg"})
SVG = b"<svg>one</svg>"
PNG = b"png-one"
SVG_TWO = b"<svg>two</svg>"
PNG_TWO = b"png-two"
sha = lambda data: hashlib.sha256(data).hexdigest()  # noqa: E731


def figure_paragraph(cx: int, cy: int, svg_rid: str = "rIdS1", png_rid: str = "rIdP1", doc_id: int = 1) -> str:
    return f"""<w:p><w:pPr><w:pStyle w:val="CaptionedFigure"/></w:pPr><w:bookmarkStart w:id="{doc_id}" w:name="fig{doc_id}"/>
<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>
<wp:docPr id="{doc_id}" name="Picture {doc_id}"/><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="p"/><pic:cNvPicPr/></pic:nvPicPr>
<pic:blipFill><a:blip r:embed="{png_rid}"><a:extLst><a:ext uri="{{96DAC541-7B7A-43D3-8B79-37D633B846F1}}">
<asvg:svgBlip r:embed="{svg_rid}"/></a:ext></a:extLst></a:blip></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm></pic:spPr></pic:pic>
</a:graphicData></a:graphic></wp:inline></w:drawing></w:r><w:bookmarkEnd w:id="{doc_id}"/></w:p>"""


def caption(style: str, text: str) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def build(body: str, extra_rels: str = "") -> tuple[OpcPackage, etree._Element]:
    document = parse_xml(f"<w:document {DECLARATIONS}><w:body>{body}</w:body></w:document>".encode())
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rIdS1" Type="t" Target="media/one.svg"/><Relationship Id="rIdP1" Type="t" Target="media/one.png"/>'
        '<Relationship Id="rIdS2" Type="t" Target="media/two.svg"/><Relationship Id="rIdP2" Type="t" Target="media/two.png"/>'
        f"{extra_rels}</Relationships>"
    )
    package = OpcPackage(
        {
            "word/_rels/document.xml.rels": rels.encode(),
            "word/media/one.svg": SVG, "word/media/one.png": PNG,
            "word/media/two.svg": SVG_TWO, "word/media/two.png": PNG_TWO,
        }
    )
    return package, document


def plan_for(document: etree._Element, layout: ResolvedLayout, body_start: etree._Element | None = None) -> SectionPlan:
    return SectionPlan(layout, document.find(qn("w:body")), body_start)


def relative(fraction: float, basis: str) -> dict:
    return {"original": "x", "kind": "relative", "fraction": fraction, "basis": basis, "inches": None, "emitted": "x"}


def absolute(inches: float) -> dict:
    return {"original": "x", "kind": "absolute", "fraction": None, "basis": None, "inches": inches, "emitted": "x"}


@pytest.fixture(scope="module")
def ieee() -> ResolvedLayout:
    return load_profile("ieee-conference").layout


@pytest.fixture(scope="module")
def generic() -> ResolvedLayout:
    return load_profile("generic").layout


def extents(document: etree._Element) -> tuple[tuple[int, int], tuple[int, int]]:
    outer = document.find(f".//{qn('wp:extent')}")
    inner = document.find(f".//{qn('pic:spPr')}/{qn('a:xfrm')}/{qn('a:ext')}")
    return (int(outer.get("cx")), int(outer.get("cy"))), (int(inner.get("cx")), int(inner.get("cy")))


def run_one(layout: ResolvedLayout, width: dict | None, cx: int = 5 * EMU_PER_INCH, cy: int = 2 * EMU_PER_INCH, aspect=None):
    package, document = build(figure_paragraph(cx, cy))
    plan = plan_for(document, layout)
    outcomes, findings = apply_figure_extents(package, document, plan, [FigureSpec("fig1", sha(SVG), width, aspect)])
    return document, outcomes, findings


def test_columnwidth_fraction_is_exact_in_two_column_body(ieee):
    document, (outcome,), findings = run_one(ieee, relative(0.98, "columnwidth"))
    column = ieee.body_section.column_width
    assert column.emu == int(3.5 * EMU_PER_INCH)
    assert outcome.cx_emu == column.scaled(0.98).emu
    assert extents(document) == ((outcome.cx_emu, outcome.cy_emu),) * 2
    assert outcome.cy_emu == round(outcome.cx_emu * 2 / 5)
    assert not outcome.clamped and outcome.matched and outcome.id == "fig1"
    assert outcome.basis == "columnwidth" and outcome.span == "column" and outcome.section == "body"
    assert outcome.cx_in == pytest.approx(0.98 * 3.5)
    assert findings == []


def test_textwidth_is_page_text_and_clamped_to_column(ieee):
    document, (outcome,), findings = run_one(ieee, relative(0.5, "textwidth"))
    assert outcome.requested_emu == ieee.page.text_width.scaled(0.5).emu  # 3.625in
    assert outcome.requested_emu > outcome.available_emu
    assert outcome.cx_emu == ieee.body_section.column_width.emu and outcome.clamped
    assert [f.code for f in findings] == ["L2W-W-LAYOUT-011"]
    message = findings[0].message
    assert "fig1" in message and "3.6250in" in message and "3.5000in" in message
    assert extents(document)[0][0] == outcome.cx_emu


def test_linewidth_resolves_to_column(ieee):
    _, (outcome,), findings = run_one(ieee, relative(0.5, "linewidth"))
    assert outcome.cx_emu == ieee.body_section.column_width.scaled(0.5).emu
    assert outcome.basis == "linewidth" and findings == []


@pytest.mark.parametrize(
    "inches",
    [3.0, 5 / 2.54, 40 / 25.4, 200 / 72.27],
)
def test_absolute_widths(ieee, inches):
    _, (outcome,), findings = run_one(ieee, absolute(inches))
    assert outcome.cx_emu == Length.inches(repr(inches)).emu
    assert outcome.basis == "absolute" and not outcome.clamped
    assert findings == []


def test_absolute_wider_than_column_is_clamped(ieee):
    _, (outcome,), findings = run_one(ieee, absolute(4.2))
    assert outcome.clamped and outcome.cx_emu == outcome.available_emu
    assert findings[0].code == "L2W-W-LAYOUT-011"


def test_single_column_layout_columnwidth_is_text_width(generic):
    _, (outcome,), findings = run_one(generic, relative(1.0, "columnwidth"), cx=3 * EMU_PER_INCH, cy=EMU_PER_INCH)
    assert outcome.cx_emu == Length.inches("6.5").emu == generic.body_section.column_width.emu
    assert outcome.cy_emu == outcome.cx_emu // 3
    assert not outcome.clamped and findings == []


def test_aspect_ratio_is_kept_from_existing_extent(ieee):
    cx, cy = 4_000_001, 1_000_003
    document, (outcome,), _ = run_one(ieee, relative(0.5, "columnwidth"), cx=cx, cy=cy)
    assert outcome.cy_emu == round(outcome.cx_emu * cy / cx)
    assert extents(document) == ((outcome.cx_emu, outcome.cy_emu),) * 2


def test_no_width_keeps_intrinsic_size(ieee):
    document, (outcome,), findings = run_one(ieee, None, cx=2 * EMU_PER_INCH, cy=EMU_PER_INCH)
    assert extents(document)[0] == (2 * EMU_PER_INCH, EMU_PER_INCH)
    assert outcome.requested_emu is None and outcome.basis is None and findings == []


def test_no_width_oversize_intrinsic_is_clamped(ieee):
    _, (outcome,), findings = run_one(ieee, None, cx=6 * EMU_PER_INCH, cy=3 * EMU_PER_INCH)
    assert outcome.clamped and outcome.cx_emu == ieee.body_section.column_width.emu
    assert outcome.cy_emu == outcome.cx_emu // 2
    assert [f.code for f in findings] == ["L2W-W-LAYOUT-011"]


def test_allow_overflow_keeps_oversize_width(ieee):
    layout = replace(ieee, figures=replace(ieee.figures, allow_overflow=True))
    _, (outcome,), findings = run_one(layout, relative(0.5, "textwidth"))
    assert outcome.cx_emu == ieee.page.text_width.scaled(0.5).emu and not outcome.clamped
    assert findings == []


def test_blips_docpr_and_bookmarks_untouched(ieee):
    package, document = build(figure_paragraph(5 * EMU_PER_INCH, 2 * EMU_PER_INCH))
    before = [etree.tostring(node) for node in xpath(document, "//a:blip | //wp:docPr | //w:bookmarkStart | //w:bookmarkEnd")]
    apply_figure_extents(package, document, plan_for(document, ieee), [FigureSpec("f", sha(SVG), relative(0.5, "columnwidth"), None)])
    after = [etree.tostring(node) for node in xpath(document, "//a:blip | //wp:docPr | //w:bookmarkStart | //w:bookmarkEnd")]
    assert before == after and len(before) == 4
    assert len(xpath(document, "//asvg:svgBlip")) == 1
    assert package.read("word/media/one.svg") == SVG


def test_unmatched_drawing_keeps_size_and_warns(ieee):
    package, document = build(figure_paragraph(5 * EMU_PER_INCH, 2 * EMU_PER_INCH))
    outcomes, findings = apply_figure_extents(package, document, plan_for(document, ieee), [FigureSpec("other", sha(b"x"), None, None)])
    assert outcomes[0].matched is False and outcomes[0].id is None
    assert [f.code for f in findings] == ["L2W-W-LAYOUT-010"]
    assert extents(document)[0] == (5 * EMU_PER_INCH, 2 * EMU_PER_INCH)


def test_hash_matching_beats_document_order(ieee):
    body = figure_paragraph(EMU_PER_INCH, EMU_PER_INCH) + figure_paragraph(EMU_PER_INCH, EMU_PER_INCH, "rIdS2", "rIdP2", 2)
    package, document = build(body)
    specs = [FigureSpec("a", sha(SVG_TWO), relative(0.5, "columnwidth"), None), FigureSpec("b", sha(SVG), relative(0.25, "columnwidth"), None)]
    outcomes, _ = apply_figure_extents(package, document, plan_for(document, ieee), specs)
    assert [o.id for o in outcomes] == ["b", "a"]


def test_specs_without_hash_match_in_document_order(ieee):
    body = figure_paragraph(EMU_PER_INCH, EMU_PER_INCH) + figure_paragraph(EMU_PER_INCH, EMU_PER_INCH, "rIdS2", "rIdP2", 2)
    package, document = build(body)
    specs = [FigureSpec("a", None, relative(0.5, "columnwidth"), None), FigureSpec("b", None, relative(0.25, "columnwidth"), None)]
    outcomes, findings = apply_figure_extents(package, document, plan_for(document, ieee), specs)
    assert [o.id for o in outcomes] == ["a", "b"] and findings == []


def test_aspect_mismatch_warns(ieee):
    _, _, findings = run_one(ieee, relative(0.5, "columnwidth"), aspect=1.0)  # drawing is 2.5:1
    assert [f.code for f in findings] == ["L2W-W-LAYOUT-012"]
    _, _, findings = run_one(ieee, relative(0.5, "columnwidth"), aspect=2.51)
    assert findings == []


def test_figure_in_frontmatter_uses_frontmatter_column(ieee):
    first = figure_paragraph(EMU_PER_INCH, EMU_PER_INCH)
    package, document = build(first + caption("ImageCaption", "c") + '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr></w:p>')
    body_start = document.find(qn("w:body"))[-1]
    plan = plan_for(document, ieee, body_start)
    (outcome,), _ = apply_figure_extents(package, document, plan, [FigureSpec("f", sha(SVG), relative(1.0, "columnwidth"), None)])
    assert outcome.section == "frontmatter"
    assert outcome.cx_emu == ieee.page.text_width.emu  # one front-matter column spans the text width


# ------------------------------------------------------------------ caption placement


def order(document: etree._Element) -> list[str]:
    out = []
    for child in document.find(qn("w:body")):
        if child.tag == w("tbl"):
            out.append("tbl")
        elif child.tag == w("p"):
            style = child.find(f"{w('pPr')}/{w('pStyle')}").get(w("val"))
            out.append(style)
        else:
            out.append(etree.QName(child).localname)
    return out


def simple_table() -> str:
    return '<w:tbl><w:tblPr><w:tblStyle w:val="Table"/></w:tblPr><w:tblGrid><w:gridCol w:w="100"/></w:tblGrid><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl>'


def test_native_order_is_kept_when_profile_matches(ieee):
    body = caption("TableCaption", "t") + simple_table() + figure_paragraph(1, 1) + caption("ImageCaption", "c")
    _, document = build(body)
    assert apply_caption_placement(document, ieee) == []
    assert order(document) == ["TableCaption", "tbl", "CaptionedFigure", "ImageCaption"]


def test_figure_caption_above_and_table_caption_below(ieee):
    layout = replace(ieee, figures=replace(ieee.figures, caption_placement="above"), tables=replace(ieee.tables, caption_placement="below"))
    body = (
        figure_paragraph(1, 1) + caption("ImageCaption", "c1") + figure_paragraph(1, 1, doc_id=2) + caption("ImageCaption", "c2")
        + caption("TableCaption", "t") + simple_table()
    )
    _, document = build(body)
    findings = apply_caption_placement(document, layout)
    assert [f.code for f in findings] == ["L2W-I-LAYOUT-014"] * 3
    assert order(document) == ["ImageCaption", "CaptionedFigure", "ImageCaption", "CaptionedFigure", "tbl", "TableCaption"]
    # bookmarks stay attached to the figure paragraph, not the caption
    for figure in xpath(document, "//w:p[w:pPr/w:pStyle/@w:val='CaptionedFigure']"):
        assert figure.find(w("bookmarkStart")) is not None
    # a second pass is a no-op
    assert apply_caption_placement(document, layout) == []


def test_swap_back_to_native_order(ieee):
    body = caption("ImageCaption", "c") + figure_paragraph(1, 1) + simple_table() + caption("TableCaption", "t")
    _, document = build(body)
    assert apply_caption_placement(document, ieee)
    assert order(document) == ["CaptionedFigure", "ImageCaption", "TableCaption", "tbl"]
