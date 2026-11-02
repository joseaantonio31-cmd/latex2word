from __future__ import annotations

import io
import subprocess
import zipfile
from dataclasses import replace

import pytest
from lxml import etree
from pandockit import PANDOC, requires_pandoc

from latex2word.errors import BuildError
from latex2word.layout import ResolvedLayout, TextStyle
from latex2word.ooxml import style_ids as ids
from latex2word.ooxml.namespaces import w
from latex2word.ooxml.package import parse_xml, serialize_xml
from latex2word.ooxml.styles import apply_typography, describe_styles, ensure_required_styles, style_ids_in
from latex2word.ooxml.xmlutil import PPR_ORDER, RPR_ORDER, STYLE_ORDER, TBLPR_ORDER, TCPR_ORDER
from latex2word.profiles import load_profile
from latex2word.units import Length

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# A hand-written part in the shape of Pandoc's: theme fonts, a theme-coloured heading, a linked character style.
SYNTHETIC = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="{W_NS}">
  <w:docDefaults>
    <w:rPrDefault><w:rPr>
      <w:rFonts w:asciiTheme="minorHAnsi" w:hAnsiTheme="minorHAnsi"/><w:sz w:val="24"/><w:szCs w:val="24"/>
    </w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr><w:spacing w:after="200"/></w:pPr></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
  <w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:before="180" w:after="180"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
    <w:next w:val="BodyText"/><w:link w:val="Heading1Char"/><w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="360" w:after="80" w:beforeAutospacing="1"/><w:outlineLvl w:val="0"/></w:pPr>
    <w:rPr><w:rFonts w:asciiTheme="majorHAnsi" w:hAnsiTheme="majorHAnsi"/>
      <w:color w:val="0F4761" w:themeColor="accent1" w:themeShade="BF"/><w:sz w:val="40"/><w:szCs w:val="40"/></w:rPr></w:style>
  <w:style w:type="character" w:customStyle="1" w:styleId="Heading1Char"><w:name w:val="Heading 1 Char"/>
    <w:basedOn w:val="DefaultParagraphFont"/><w:link w:val="Heading1"/>
    <w:rPr><w:color w:val="0F4761" w:themeColor="accent1"/><w:sz w:val="40"/></w:rPr></w:style>
  <w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont"><w:name w:val="Default Paragraph Font"/></w:style>
</w:styles>"""


def pandoc_default_styles() -> etree._Element:
    completed = subprocess.run(
        [PANDOC, "--print-default-data-file", "reference.docx"], capture_output=True, check=True, stdin=subprocess.DEVNULL
    )
    with zipfile.ZipFile(io.BytesIO(completed.stdout)) as archive:
        return parse_xml(archive.read("word/styles.xml"))


@pytest.fixture(params=["synthetic", pytest.param("pandoc", marks=requires_pandoc)])
def base_styles(request: pytest.FixtureRequest) -> etree._Element:
    return parse_xml(SYNTHETIC.encode()) if request.param == "synthetic" else pandoc_default_styles()


@pytest.fixture(scope="module")
def ieee() -> ResolvedLayout:
    layout = load_profile("ieee-conference").layout
    assert layout is not None
    return layout


@pytest.fixture(scope="module")
def generic() -> ResolvedLayout:
    layout = load_profile("generic").layout
    assert layout is not None
    return layout


def style_of(styles: etree._Element, style_id: str) -> etree._Element:
    found = [s for s in styles.iterfind(w("style")) if s.get(w("styleId")) == style_id]
    assert len(found) == 1, style_id
    return found[0]


def prop(styles: etree._Element, style_id: str, container: str, name: str) -> etree._Element | None:
    return style_of(styles, style_id).find(f"{w(container)}/{w(name)}")


def val(styles: etree._Element, style_id: str, container: str, name: str, attribute: str = "val") -> str | None:
    node = prop(styles, style_id, container, name)
    return None if node is None else node.get(w(attribute))


def with_typography(layout: ResolvedLayout, **roles: TextStyle) -> ResolvedLayout:
    return replace(layout, typography={**layout.typography, **roles})


def local(element: etree._Element) -> str:
    return etree.QName(element).localname


def assert_schema_order(root: etree._Element) -> None:
    orders = {"pPr": PPR_ORDER, "rPr": RPR_ORDER, "tblPr": TBLPR_ORDER, "tcPr": TCPR_ORDER}
    for element in root.iter(w("style"), w("pPr"), w("rPr"), w("tblPr"), w("tcPr")):
        order = STYLE_ORDER if local(element) == "style" else orders[local(element)]
        names = [local(child) for child in element]
        unknown = [name for name in names if name not in order and name not in {"tblStylePr"}]
        assert not unknown, f"{local(element)} has children outside the schema order: {unknown}"
        ranks = [order.index(name) for name in names if name in order]
        assert ranks == sorted(ranks), f"{local(element)} children out of schema order: {names}"


# --------------------------------------------------------------------------- required styles and numbers


def test_every_required_style_exists_after_apply_typography(base_styles, ieee):
    apply_typography(base_styles, ieee)
    defined = style_ids_in(base_styles)
    assert set(ids.REQUIRED_STYLES) <= defined
    assert {ids.ABSTRACT_LABEL, ids.KEYWORDS_LABEL, ids.AUTHOR_AFFILIATION, ids.AUTHOR_EMAIL} <= defined
    assert {ids.TABLE, ids.TABLE_EQUATION_LAYOUT, ids.TABLE_AUTHOR_BLOCK} <= defined


def test_created_styles_are_reported_and_character_styles_are_typed(base_styles, ieee):
    before = style_ids_in(base_styles)
    created = apply_typography(base_styles, ieee)
    assert created and set(created) == style_ids_in(base_styles) - before
    assert style_of(base_styles, ids.ABSTRACT_LABEL).get(w("type")) == "character"
    assert prop(base_styles, ids.ABSTRACT_LABEL, "rPr", "b") is not None
    affiliation = style_of(base_styles, ids.AUTHOR_AFFILIATION)
    assert affiliation.get(w("customStyle")) == "1" and affiliation.find(w("name")).get(w("val")) == "Author Affiliation"
    assert style_of(base_styles, ids.EQUATION_SPACER).find(w("basedOn")).get(w("val")) == ids.EQUATION
    assert apply_typography(base_styles, ieee) == []


def test_ieee_numbers_reach_the_styles_exactly(base_styles, ieee):
    apply_typography(base_styles, ieee)
    heading1 = style_of(base_styles, "Heading1")
    assert (val(base_styles, "Heading1", "rPr", "sz"), val(base_styles, "Heading1", "rPr", "szCs")) == ("20", "20")
    assert prop(base_styles, "Heading1", "rPr", "smallCaps").get(w("val")) is None  # on
    assert val(base_styles, "Heading1", "rPr", "caps") == "0"
    assert val(base_styles, "Heading1", "rPr", "b") == "0" and val(base_styles, "Heading1", "rPr", "i") == "0"
    assert val(base_styles, "Heading1", "pPr", "jc") == "center"
    assert (val(base_styles, "Heading1", "pPr", "spacing", "before"), val(base_styles, "Heading1", "pPr", "spacing", "after")) == ("240", "120")
    assert prop(base_styles, "Heading1", "pPr", "keepNext") is not None and prop(base_styles, "Heading1", "pPr", "keepLines") is not None
    assert val(base_styles, "Heading1", "pPr", "spacing", "beforeAutospacing") is None
    assert val(base_styles, "Heading1", "pPr", "outlineLvl") == "0"
    assert heading1.find(f"{w('next')}").get(w("val")) == "BodyText"

    assert val(base_styles, "Heading2", "rPr", "sz") == "20" and prop(base_styles, "Heading2", "rPr", "i").get(w("val")) is None
    assert val(base_styles, "Heading2", "rPr", "smallCaps") == "0" and val(base_styles, "Heading2", "pPr", "jc") == "left"
    assert (val(base_styles, "Heading2", "pPr", "spacing", "before"), val(base_styles, "Heading2", "pPr", "spacing", "after")) == ("120", "60")

    assert val(base_styles, "Bibliography", "rPr", "sz") == "16"
    assert val(base_styles, "Bibliography", "pPr", "jc") == "both"
    assert (val(base_styles, "Bibliography", "pPr", "ind", "left"), val(base_styles, "Bibliography", "pPr", "ind", "hanging")) == (
        str(ieee.typography["bibliography"].indent_left.twips), str(ieee.typography["bibliography"].indent_hanging.twips)
    )
    assert val(base_styles, "Bibliography", "pPr", "ind", "firstLine") is None
    assert val(base_styles, "Bibliography", "pPr", "spacing", "after") == "40"

    assert val(base_styles, "TableCaption", "rPr", "sz") == "16"
    assert prop(base_styles, "TableCaption", "rPr", "smallCaps").get(w("val")) is None
    assert val(base_styles, "TableCaption", "pPr", "jc") == "center"
    assert (val(base_styles, "TableCaption", "pPr", "spacing", "before"), val(base_styles, "TableCaption", "pPr", "spacing", "after")) == ("160", "60")
    assert prop(base_styles, "TableCaption", "pPr", "keepNext") is not None


def test_describe_styles_reports_effective_values(base_styles, ieee):
    apply_typography(base_styles, ieee)
    described = describe_styles(base_styles, ["Heading1", "Heading2", "Bibliography", "TableCaption", "BodyText", "Missing"])
    assert "Missing" not in described
    assert described["Heading1"] == {
        "name": "heading 1", "basedOn": "Normal", "font": "Times New Roman", "size_pt": 10.0, "bold": False, "italic": False,
        "caps": "small", "alignment": "center", "before_pt": 12.0, "after_pt": 6.0, "keep_next": True,
    }
    assert (described["Heading2"]["italic"], described["Heading2"]["caps"]) == (True, "none")
    assert described["Bibliography"]["alignment"] == "justify" and described["Bibliography"]["size_pt"] == 8.0
    assert described["TableCaption"]["caps"] == "small" and described["TableCaption"]["keep_next"] is True
    assert described["BodyText"]["alignment"] == "justify" and described["BodyText"]["font"] == "Times New Roman"


def test_describe_styles_resolves_basedon_and_defaults():
    styles = parse_xml(SYNTHETIC.encode())
    described = describe_styles(styles, ["BodyText", "Heading1Char"])
    assert described["BodyText"]["before_pt"] == 9.0 and described["BodyText"]["size_pt"] == 12.0
    assert described["BodyText"]["font"] is None  # theme-only default font: no explicit ascii face
    assert described["Heading1Char"]["basedOn"] == "DefaultParagraphFont"


def test_schema_order_holds_everywhere(base_styles, ieee, generic):
    apply_typography(base_styles, ieee)
    assert_schema_order(base_styles)
    other = parse_xml(SYNTHETIC.encode())
    apply_typography(other, generic)
    assert_schema_order(other)


def test_apply_typography_is_idempotent(base_styles, ieee):
    apply_typography(base_styles, ieee)
    first = serialize_xml(base_styles)
    assert apply_typography(base_styles, ieee) == []
    assert serialize_xml(base_styles) == first


# --------------------------------------------------------------------------- theme leakage


def test_theme_colours_and_fonts_are_removed_from_headings_and_title(base_styles, ieee):
    apply_typography(base_styles, ieee)
    for style in base_styles.iterfind(w("style")):
        if style.get(w("styleId")).startswith("Heading") or style.get(w("styleId")) in {"Title", "TitleChar", "Subtitle", "SubtitleChar"}:
            for node in style.iter(w("color"), w("rFonts")):
                theme = [a for a in node.attrib if a.endswith(("Theme", "themeColor", "themeShade", "themeTint", "cstheme"))]
                assert not theme, (style.get(w("styleId")), theme)
    assert prop(base_styles, "Heading1", "rPr", "color") is None
    assert prop(base_styles, "Heading1", "rPr", "rFonts") is None  # inherits Times New Roman from Normal
    assert val(base_styles, "Heading1Char", "rPr", "sz") == "20"


def test_profile_colour_and_font_replace_the_theme(base_styles, generic):
    layout = with_typography(generic, heading1=TextStyle(font="Arial", color="1F3864"))
    apply_typography(base_styles, layout)
    fonts = prop(base_styles, "Heading1", "rPr", "rFonts")
    assert {local_name.split("}")[1]: v for local_name, v in fonts.attrib.items()} == {
        "ascii": "Arial", "hAnsi": "Arial", "cs": "Arial", "eastAsia": "Arial"
    }
    color = prop(base_styles, "Heading1", "rPr", "color")
    assert dict(color.attrib) == {w("val"): "1F3864"}
    assert val(base_styles, "Heading1Char", "rPr", "color") == "1F3864"


# --------------------------------------------------------------------------- TextStyle -> OOXML


def apply_single(role: str, text: TextStyle, layout: ResolvedLayout) -> etree._Element:
    styles = parse_xml(SYNTHETIC.encode())
    apply_typography(styles, with_typography(layout, **{role: text}))
    return styles


def test_properties_are_written_in_word_units(generic):
    text = TextStyle(
        font="Georgia", size_pt=9.25, bold=True, italic=False, color="auto", caps="all", alignment="justify",
        before_pt=2.5, after_pt=0.1, line_spacing=1.15, keep_next=False, keep_lines=True, widow_control=False,
        indent_left=Length.points(18), indent_first_line=Length.points(10),
    )
    styles = apply_single("abstract", text, generic)
    assert val(styles, "Abstract", "rPr", "sz") == "19"  # 18.5 half-points round half up, not to even
    assert val(styles, "Abstract", "rPr", "rFonts", "ascii") == "Georgia" and val(styles, "Abstract", "rPr", "rFonts", "eastAsia") == "Georgia"
    assert prop(styles, "Abstract", "rPr", "b").get(w("val")) is None and prop(styles, "Abstract", "rPr", "bCs") is not None
    assert val(styles, "Abstract", "rPr", "i") == "0" and val(styles, "Abstract", "rPr", "iCs") == "0"
    assert val(styles, "Abstract", "rPr", "color") == "auto"
    assert prop(styles, "Abstract", "rPr", "caps").get(w("val")) is None and val(styles, "Abstract", "rPr", "smallCaps") == "0"
    assert val(styles, "Abstract", "pPr", "jc") == "both"
    spacing = prop(styles, "Abstract", "pPr", "spacing")
    assert (spacing.get(w("before")), spacing.get(w("after")), spacing.get(w("line")), spacing.get(w("lineRule"))) == ("50", "2", "276", "auto")
    assert val(styles, "Abstract", "pPr", "keepNext") == "0" and prop(styles, "Abstract", "pPr", "keepLines").get(w("val")) is None
    assert val(styles, "Abstract", "pPr", "widowControl") == "0"
    indent = prop(styles, "Abstract", "pPr", "ind")
    assert (indent.get(w("left")), indent.get(w("firstLine")), indent.get(w("hanging"))) == ("360", "200", None)
    assert_schema_order(styles)


def test_unset_fields_leave_a_style_untouched(generic):
    styles = parse_xml(SYNTHETIC.encode())
    before = serialize_xml(style_of(styles, "Heading1"))
    layout = replace(generic, typography={k: v for k, v in generic.typography.items() if k != "heading1"})
    apply_typography(styles, layout)
    assert val(styles, "Heading1", "pPr", "spacing", "before") == "360"
    assert val(styles, "Heading1", "rPr", "sz") == "40"
    # the theme colour is still stripped: it is Pandoc's, not a profile choice
    assert prop(styles, "Heading1", "rPr", "color") is None
    assert serialize_xml(style_of(styles, "Heading1")) != before


def test_hanging_replaces_first_line_and_autospacing_goes(generic):
    styles = parse_xml(SYNTHETIC.encode())
    heading = style_of(styles, "Heading1")
    heading.find(f"{w('pPr')}/{w('spacing')}").set(w("afterAutospacing"), "1")
    etree.SubElement(heading.find(w("pPr")), w("ind")).set(w("firstLine"), "240")
    heading.find(w("pPr")).remove(heading.find(f"{w('pPr')}/{w('outlineLvl')}"))
    etree.SubElement(heading.find(w("pPr")), w("outlineLvl")).set(w("val"), "0")
    layout = with_typography(generic, heading1=TextStyle(indent_hanging=Length.points(18), after_pt=6, before_pt=12))
    apply_typography(styles, layout)
    indent = prop(styles, "Heading1", "pPr", "ind")
    assert (indent.get(w("hanging")), indent.get(w("firstLine"))) == ("360", None)
    spacing = prop(styles, "Heading1", "pPr", "spacing")
    assert spacing.get(w("afterAutospacing")) is None and spacing.get(w("beforeAutospacing")) is None
    assert_schema_order(styles)


def test_first_line_and_hanging_together_are_an_error(generic):
    layout = with_typography(generic, heading1=TextStyle(indent_hanging=Length.points(9), indent_first_line=Length.points(9)))
    with pytest.raises(BuildError, match="L2W-E-REFDOC-005"):
        apply_typography(parse_xml(SYNTHETIC.encode()), layout)


def test_character_styles_cannot_carry_paragraph_properties(generic):
    layout = with_typography(generic, abstract_label=TextStyle(alignment="center"))
    with pytest.raises(BuildError, match="L2W-E-REFDOC-006"):
        apply_typography(parse_xml(SYNTHETIC.encode()), layout)


# --------------------------------------------------------------------------- defaults, body, compact, figure


def test_document_defaults_follow_the_profile(base_styles, ieee):
    apply_typography(base_styles, ieee)
    defaults = base_styles.find(w("docDefaults"))
    fonts = defaults.find(f"{w('rPrDefault')}/{w('rPr')}/{w('rFonts')}")
    assert {k.split("}")[1]: v for k, v in fonts.attrib.items()} == {
        "ascii": "Times New Roman", "hAnsi": "Times New Roman", "cs": "Times New Roman", "eastAsia": "Times New Roman"
    }
    assert defaults.find(f"{w('rPrDefault')}/{w('rPr')}/{w('sz')}").get(w("val")) == "20"
    spacing = defaults.find(f"{w('pPrDefault')}/{w('pPr')}/{w('spacing')}")
    assert (spacing.get(w("before")), spacing.get(w("after")), spacing.get(w("line"))) == ("0", "0", "240")
    assert val(base_styles, "Normal", "rPr", "sz") == "20"


def test_body_paragraph_defaults_apply_unless_the_body_role_sets_them(generic):
    """generic: ``body`` sets only the alignment, so ``layout.paragraph`` supplies the spacing (6 pt after)."""
    styles = parse_xml(SYNTHETIC.encode())
    apply_typography(styles, generic)
    for style_id in (ids.BODY_TEXT, ids.FIRST_PARAGRAPH):
        spacing = prop(styles, style_id, "pPr", "spacing")
        assert (spacing.get(w("before")), spacing.get(w("after")), spacing.get(w("line")), spacing.get(w("lineRule"))) == ("0", "120", "240", "auto")
        assert val(styles, style_id, "pPr", "jc") == "both"
    overriding = with_typography(generic, body=TextStyle(after_pt=2))
    styles = parse_xml(SYNTHETIC.encode())
    apply_typography(styles, overriding)
    assert val(styles, ids.BODY_TEXT, "pPr", "spacing", "after") == "40"
    assert val(styles, ids.BODY_TEXT, "pPr", "spacing", "before") == "0"


def test_compact_follows_table_text_and_drops_the_body_indent(base_styles, ieee):
    apply_typography(base_styles, ieee)
    assert val(base_styles, "Compact", "rPr", "sz") == "16"
    assert val(base_styles, "Compact", "pPr", "jc") == "left"
    assert (val(base_styles, "Compact", "pPr", "spacing", "before"), val(base_styles, "Compact", "pPr", "spacing", "after")) == ("20", "20")
    assert val(base_styles, "Compact", "pPr", "ind", "firstLine") == "0"  # BodyText indents first lines by 10 pt


def test_captioned_figure_uses_the_figure_alignment(base_styles, ieee):
    layout = replace(ieee, figures=replace(ieee.figures, alignment="right"))
    apply_typography(base_styles, layout)
    figure = style_of(base_styles, "CaptionedFigure")
    assert figure.find(f"{w('pPr')}/{w('jc')}").get(w("val")) == "right"
    assert figure.find(f"{w('pPr')}/{w('keepNext')}") is not None
    assert val(base_styles, "CaptionedFigure", "pPr", "spacing", "after") == "0"


def test_captioned_figure_is_created_on_top_of_figure():
    styles = parse_xml(SYNTHETIC.replace("</w:styles>", '<w:style w:type="paragraph" w:customStyle="1" w:styleId="Figure"><w:name w:val="Figure"/></w:style></w:styles>').encode())
    layout = load_profile("generic").layout
    assert "CaptionedFigure" in apply_typography(styles, layout)
    assert style_of(styles, "CaptionedFigure").find(w("basedOn")).get(w("val")) == "Figure"


def test_headings_created_when_missing_carry_outline_level_and_next(generic):
    styles = parse_xml(f'<w:styles xmlns:w="{W_NS}"><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style></w:styles>'.encode())
    apply_typography(styles, generic)
    heading2 = style_of(styles, "Heading2")
    assert heading2.get(w("customStyle")) is None and heading2.find(f"{w('pPr')}/{w('outlineLvl')}").get(w("val")) == "1"
    assert heading2.find(w("next")).get(w("val")) == "BodyText"
    assert_schema_order(styles)


# --------------------------------------------------------------------------- table styles


def table_style_xml(layout: ResolvedLayout, style_id: str = ids.TABLE) -> etree._Element:
    styles = parse_xml(SYNTHETIC.encode())
    apply_typography(styles, layout)
    return style_of(styles, style_id)


def border_map(style: etree._Element) -> dict[str, tuple[str, str | None]]:
    return {local(b): (b.get(w("val")), b.get(w("sz"))) for b in style.find(f"{w('tblPr')}/{w('tblBorders')}")}


def test_rules_table_style_from_ieee_profile(ieee):
    style = table_style_xml(ieee)
    assert style.get(w("type")) == "table"
    assert border_map(style) == {
        "top": ("single", "8"), "left": ("nil", None), "bottom": ("single", "8"), "right": ("nil", None),
        "insideH": ("nil", None), "insideV": ("nil", None),
    }
    margins = {local(m): (m.get(w("w")), m.get(w("type"))) for m in style.find(f"{w('tblPr')}/{w('tblCellMar')}")}
    assert margins == {"top": ("14", "dxa"), "left": ("58", "dxa"), "bottom": ("14", "dxa"), "right": ("58", "dxa")}
    first_row = style.find(w("tblStylePr"))
    assert first_row.get(w("type")) == "firstRow"
    assert first_row.find(f"{w('tcPr')}/{w('tcBorders')}/{w('bottom')}").get(w("sz")) == "4"
    assert first_row.find(w("rPr")) is None  # IEEE: header emphasis none


def test_grid_and_none_borders_and_bold_header(generic):
    grid = table_style_xml(replace(generic, tables=replace(generic.tables, borders="grid", header_emphasis="bold")))
    assert set(border_map(grid).values()) == {("single", "4")} and len(border_map(grid)) == 6
    header = grid.find(w("tblStylePr"))
    assert header.find(f"{w('rPr')}/{w('b')}") is not None
    assert header.find(f"{w('tcPr')}/{w('tcBorders')}") is None
    none = table_style_xml(replace(generic, tables=replace(generic.tables, borders="none", header_emphasis="none")))
    assert set(border_map(none).values()) == {("nil", None)}
    assert none.find(f"{w('tblStylePr')}/{w('rPr')}") is None


def test_layout_table_styles_are_borderless_with_the_documented_margins(ieee):
    for style_id, side in ((ids.TABLE_EQUATION_LAYOUT, "0"), (ids.TABLE_AUTHOR_BLOCK, "60")):
        style = table_style_xml(ieee, style_id)
        assert set(border_map(style).values()) == {("nil", None)}
        margins = {local(m): m.get(w("w")) for m in style.find(f"{w('tblPr')}/{w('tblCellMar')}")}
        assert margins == {"top": "0", "left": side, "bottom": "0", "right": side}
        assert style.find(w("tblStylePr")) is None


def test_table_style_overwrite_keeps_the_default_flag_and_position():
    styles = parse_xml(
        SYNTHETIC.replace("</w:styles>", '<w:style w:type="table" w:default="1" w:styleId="Table"><w:name w:val="Table"/>'
                          '<w:tblPr><w:tblBorders><w:top w:val="double"/></w:tblBorders></w:tblPr></w:style>'
                          '<w:style w:type="paragraph" w:styleId="Tail"><w:name w:val="Tail"/></w:style></w:styles>').encode()
    )
    layout = load_profile("generic").layout
    created = apply_typography(styles, layout)
    assert "Table" not in created
    ordered = [s.get(w("styleId")) for s in styles.iterfind(w("style"))]
    assert ordered.index("Table") < ordered.index("Tail")
    assert style_of(styles, "Table").get(w("default")) == "1"
    assert [s.get(w("styleId")) for s in styles.iterfind(w("style"))].count("Table") == 1
    assert_schema_order(styles)


# --------------------------------------------------------------------------- external templates


def test_ensure_required_styles_only_adds_what_is_missing(base_styles, ieee):
    before = {sid: serialize_xml(style_of(base_styles, sid)) for sid in style_ids_in(base_styles)}
    defaults_before = serialize_xml(base_styles.find(w("docDefaults")))
    created = ensure_required_styles(base_styles, ieee)
    assert created
    assert set(created) == style_ids_in(base_styles) - set(before)
    assert set(ids.REQUIRED_STYLES) <= style_ids_in(base_styles)
    for style_id, xml in before.items():
        assert serialize_xml(style_of(base_styles, style_id)) == xml, f"{style_id} was modified"
    assert serialize_xml(base_styles.find(w("docDefaults"))) == defaults_before
    assert ensure_required_styles(base_styles, ieee) == []


def test_ensure_required_styles_initialises_new_styles_from_the_layout(ieee):
    styles = parse_xml(f'<w:styles xmlns:w="{W_NS}"><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style></w:styles>'.encode())
    created = ensure_required_styles(styles, ieee)
    assert ids.EQUATION_NUMBER in created and ids.TABLE in created
    assert val(styles, "Heading1", "rPr", "sz") == "20" and val(styles, "EquationNumber", "pPr", "jc") == "right"
    assert style_of(styles, ids.TABLE).get(w("type")) == "table"
    assert_schema_order(styles)


def test_ensure_required_styles_on_a_complete_part_changes_nothing(base_styles, ieee):
    apply_typography(base_styles, ieee)
    snapshot = serialize_xml(base_styles)
    assert ensure_required_styles(base_styles, ieee) == []
    assert serialize_xml(base_styles) == snapshot


def test_styles_are_found_by_id_not_by_display_name(ieee):
    styles = parse_xml(SYNTHETIC.replace('w:val="heading 1"', 'w:val="Überschrift 1"').encode())
    apply_typography(styles, ieee)
    assert val(styles, "Heading1", "rPr", "sz") == "20"
    assert style_of(styles, "Heading1").find(w("name")).get(w("val")) == "Überschrift 1"
    assert [s.get(w("styleId")) for s in styles.iterfind(w("style"))].count("Heading1") == 1
