"""The single finalisation pass: atomic replacement, source preservation, structural failures, layout validation."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from layoutkit import DEFAULT_AUTHORS, IEEE_AUTHORS, IEEE_KEYWORDS, FinalDoc, article, build_project, problems
from lxml import etree
from pandockit import requires_pandoc

from latex2word.diagnostics import Severity
from latex2word.metadata.model import Author, DocumentMetadata
from latex2word.ooxml.bibliography import apply_bibliography_layout
from latex2word.ooxml.finalize import FinalizeContext, finalize_docx
from latex2word.ooxml.namespaces import w
from latex2word.ooxml.package import OpcPackage
from latex2word.ooxml.sections import SectionPlan, apply_section_properties, find_body_start
from latex2word.ooxml.validation import LayoutExpectations, validate_layout
from latex2word.profiles import load_profile
from latex2word.source_graph import Finding

pytestmark = requires_pandoc


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("finalize")
    result = build_project(root, article(author=IEEE_AUTHORS, keywords=IEEE_KEYWORDS), "ieee-conference")
    assert result.ok, problems(result)
    report = json.loads((result.workspace / "build" / "filter_report.json").read_text(encoding="utf-8"))
    return result, [dict(s) for s in report["sequences"]["sections"]]


def context(profile_id: str, numbered, *, authors: tuple[Author, ...] = ()) -> FinalizeContext:
    profile = load_profile(profile_id)
    return FinalizeContext(profile.layout, profile.frontmatter, DocumentMetadata(title="T", authors=authors), (), numbered)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_finalisation_never_modifies_the_preliminary_docx(built, tmp_path: Path):
    result, numbered = built
    before = sha(result.preliminary)
    outcome = finalize_docx(result.preliminary, tmp_path / "final.docx", context("ieee-conference", numbered))
    assert outcome.ok and (tmp_path / "final.docx").is_file()
    assert sha(result.preliminary) == before and sha(tmp_path / "final.docx") != before
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_a_failed_validation_leaves_the_previous_final_output_byte_identical(built, tmp_path: Path):
    result, numbered = built
    destination = tmp_path / "final.docx"
    destination.write_bytes(b"previous successful output")
    bad = lambda _path: [Finding("L2W-E-TEST-001", Severity.ERROR, "rejected by the validator")]  # noqa: E731
    outcome = finalize_docx(result.preliminary, destination, context("ieee-conference", numbered), validate=bad)
    assert not outcome.ok and outcome.findings[-1].code == "L2W-E-TEST-001"
    assert destination.read_bytes() == b"previous successful output"
    assert [p.name for p in tmp_path.iterdir()] == ["final.docx"]  # no temporary file left behind


def test_a_missing_body_start_fails_without_touching_the_destination(built, tmp_path: Path):
    result, _ = built
    destination = tmp_path / "final.docx"
    destination.write_bytes(b"old")
    outcome = finalize_docx(result.preliminary, destination, context("ieee-conference", []))  # no numbered section is known
    assert not outcome.ok and outcome.errors[0].code == "L2W-E-LAYOUT-060"
    assert destination.read_bytes() == b"old"


def test_an_unreadable_preliminary_is_reported_not_raised(tmp_path: Path):
    bogus = tmp_path / "bogus.docx"
    bogus.write_bytes(b"not a zip")
    outcome = finalize_docx(bogus, tmp_path / "final.docx", context("generic", []))
    assert not outcome.ok and outcome.errors[0].code == "L2W-E-LAYOUT-061" and not (tmp_path / "final.docx").exists()


# ---------------------------------------------------------------------------- layout validation catches damage


def expectations(result, numbered) -> LayoutExpectations:
    profile = load_profile("ieee-conference")
    authors = (Author("Ada Example", email="ada@example.org"),)
    return LayoutExpectations(
        profile.layout, profile.frontmatter, DocumentMetadata(title="x", authors=authors), expect_abstract=True,
        expect_keywords=True, display_equations=1, numbered_sections=numbered,
    )


def mutated(result, tmp_path: Path, edit) -> Path:
    package = OpcPackage.load(result.final)
    edit(package, package.xml("word/document.xml"))
    path = tmp_path / "mutated.docx"
    path.write_bytes(package.to_bytes())
    return path


def codes(validation) -> set[str]:
    return {f.code for f in validation.findings if f.severity is Severity.ERROR}


def test_the_untouched_final_document_passes_layout_validation(built):
    result, numbered = built
    validation = validate_layout(result.final, expectations(result, numbered))
    assert validation.ok, [str(f) for f in validation.findings]
    assert validation.metrics["section_count"] == 2 and validation.metrics["schema_order_violations"] == {}


def test_wrong_columns_page_size_and_margins_are_detected(built, tmp_path: Path):
    result, numbered = built

    def edit(package, document):
        sects = list(document.iter(w("sectPr")))
        sects[-1].find(w("cols")).set(w("num"), "3")
        sects[0].find(w("pgSz")).set(w("w"), "11906")
        sects[0].find(w("pgMar")).set(w("left"), "1000")

    found = codes(validate_layout(mutated(result, tmp_path, edit), expectations(result, numbered)))
    assert {"L2W-E-LAYOUT-070", "L2W-E-LAYOUT-071", "L2W-E-LAYOUT-073"} <= found


def test_a_missing_second_section_is_detected(built, tmp_path: Path):
    result, numbered = built

    def edit(package, document):
        carrier = next(p for p in document.iter(w("p")) if p.find(f"{w('pPr')}/{w('sectPr')}") is not None)
        carrier.find(w("pPr")).remove(carrier.find(f"{w('pPr')}/{w('sectPr')}"))

    assert "L2W-E-LAYOUT-072" in codes(validate_layout(mutated(result, tmp_path, edit), expectations(result, numbered)))


def test_a_marker_paragraph_and_a_missing_title_and_abstract_are_detected(built, tmp_path: Path):
    result, numbered = built

    def edit(package, document):
        body = document.find(w("body"))
        marker = etree.SubElement(body, w("p"))
        body.insert(0, marker)
        etree.SubElement(etree.SubElement(marker, w("r")), w("t")).text = "FRONT_MATTER_END"
        for p in list(document.iter(w("p"))):
            style = p.find(f"{w('pPr')}/{w('pStyle')}")
            if style is not None and style.get(w("val")) in ("Title", "Abstract"):
                p.getparent().remove(p)

    found = codes(validate_layout(mutated(result, tmp_path, edit), expectations(result, numbered)))
    assert {"L2W-E-LAYOUT-083", "L2W-E-LAYOUT-075", "L2W-E-LAYOUT-077"} <= found


def test_a_wide_figure_and_wide_table_and_missing_author_are_detected(built, tmp_path: Path):
    result, numbered = built
    wp = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"

    def edit(package, document):
        extent = next(document.iter(wp + "extent"))
        extent.set("cx", str(7 * 914400))
        table = next(t for t in document.iter(w("tbl")) if t.find(f"{w('tblPr')}/{w('tblStyle')}").get(w("val")) == "Table")
        table.find(f"{w('tblPr')}/{w('tblW')}").set(w("w"), "9999")
        for grid in table.iter(w("gridCol")):
            grid.set(w("w"), "4000")
        for node in document.iter(w("t")):
            if node.text and "Ada" in node.text:
                node.text = node.text.replace("Ada", "Xx")

    found = codes(validate_layout(mutated(result, tmp_path, edit), expectations(result, numbered)))
    assert {"L2W-E-LAYOUT-080", "L2W-E-LAYOUT-082", "L2W-E-LAYOUT-076"} <= found


def test_property_children_out_of_schema_order_are_detected(built, tmp_path: Path):
    result, numbered = built

    def edit(package, document):
        properties = next(p for p in document.iter(w("pPr")) if p.find(w("pStyle")) is not None)
        spacing = etree.Element(w("spacing"))
        properties.insert(0, spacing)  # w:spacing may not precede w:pStyle

    assert "L2W-E-LAYOUT-090" in codes(validate_layout(mutated(result, tmp_path, edit), expectations(result, numbered)))


def test_an_equation_number_without_an_equation_is_detected(built, tmp_path: Path):
    result, numbered = built

    def edit(package, document):
        for math in list(document.iter("{http://schemas.openxmlformats.org/officeDocument/2006/math}oMathPara")):
            math.getparent().remove(math)

    assert "L2W-E-LAYOUT-084" in codes(validate_layout(mutated(result, tmp_path, edit), expectations(result, numbered)))


# ---------------------------------------------------------------------------- sections and bibliography helpers


def doc_body(xml: str) -> etree._Element:
    root = etree.fromstring(
        f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{xml}<w:sectPr/></w:body></w:document>'
    )
    return root.find(w("body"))


def heading(level: int, text: str) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def test_the_body_starts_at_the_first_numbered_level_one_heading_by_structure():
    body = doc_body(heading(1, "Preface") + heading(1, "I. Alpha") + heading(1, "II. Beta"))
    numbered = [{"level": 1, "text": "I.", "title": "Alpha"}, {"level": 1, "text": "II.", "title": "Beta"}]
    assert "".join(find_body_start(body, "first-numbered-heading", numbered).itertext()) == "I. Alpha"  # not the unnumbered Preface
    assert "".join(find_body_start(body, "first-heading", numbered).itertext()) == "Preface"
    assert find_body_start(body, "first-numbered-heading", []) is None


def test_a_section_break_after_a_table_gets_a_carrier_paragraph_not_a_marker():
    layout = load_profile("ieee-conference").layout
    body = doc_body('<w:tbl><w:tr/></w:tbl>' + heading(1, "I. Alpha"))
    start = body[1]
    assert apply_section_properties(body, layout, start) == 2
    carrier = body[1]
    assert carrier.tag == w("p") and len(carrier) == 1 and carrier.find(f"{w('pPr')}/{w('sectPr')}") is not None  # empty, no text
    assert SectionPlan(layout, body, start).is_frontmatter(body[0]) and not SectionPlan(layout, body, start).is_frontmatter(start)


def test_a_one_column_layout_writes_a_single_section():
    layout = load_profile("generic").layout
    body = doc_body(heading(1, "1 Alpha"))
    assert apply_section_properties(body, layout, None) == 1
    assert len(list(body.iter(w("sectPr")))) == 1 and body.find(f"{w('sectPr')}/{w('type')}") is None


def test_bibliography_tabs_become_real_tabs_and_text_is_unchanged():
    body = doc_body(
        '<w:p><w:pPr><w:pStyle w:val="Bibliography"/><w:ind w:left="720" w:hanging="720"/></w:pPr>'
        '<w:r><w:t>[1]</w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t xml:space="preserve">\tA. Writer</w:t></w:r></w:p>'
    )
    outcome = apply_bibliography_layout(body)
    assert (outcome.entries, outcome.tabs_converted, outcome.direct_formats_removed) == (1, 1, 1)
    paragraph = body[0]
    assert paragraph.find(f"{w('pPr')}/{w('ind')}") is None
    assert [etree.QName(r[-1]).localname for r in paragraph.findall(w("r"))] == ["t", "tab", "t"]
    assert "".join(t.text for t in paragraph.iter(w("t"))) == "[1]A. Writer"
    assert paragraph.findall(w("r"))[1].find(w("rPr")) is not None  # run formatting is carried to the split runs


def test_zip_output_is_deterministic_and_has_no_duplicate_entries(built):
    result, _ = built
    with zipfile.ZipFile(result.final) as archive:
        names = archive.namelist()
        assert names[0] == "[Content_Types].xml" and len(names) == len(set(names))
        assert {i.date_time for i in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_unconfigured_keyword_environments_are_not_given_the_keyword_style(tmp_path: Path):
    text = article(author=DEFAULT_AUTHORS, keywords="\\begin{IEEEkeywords}a, b\\end{IEEEkeywords}")
    result = build_project(tmp_path, text, "generic")  # generic profile declares only 'keywords'
    assert result.ok, problems(result)
    assert FinalDoc.open(result.final).paragraphs("IndexTerms") == []
