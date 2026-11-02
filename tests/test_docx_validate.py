"""Tests of the DOCX validator on synthetic packages (plus Pandoc smoke tests)."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from latex2word.docx_validate import (
    DocxExpectations,
    DocxValidation,
    FigureExpectation,
    validate_docx,
)

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main"'
)

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="png" ContentType="image/png"/>'
    '<Default Extension="svg" ContentType="image/svg+xml"/>'
    "</Types>"
)

ROOT_RELS = (
    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="x/officeDocument" Target="word/document.xml"/></Relationships>'
)

NUMBERING: dict[str, Any] = {
    "sections": {
        "enabled": True,
        "separator": " ",
        "level1": {"numeral": "Roman", "format": "{n}."},
        "level2": {"numeral": "Alpha", "format": "{n}."},
    },
    "figures": {"enabled": True, "numeral": "arabic", "format": "Fig. {n}.", "reset": "none"},
    "tables": {"enabled": True, "numeral": "Roman", "format": "TABLE {n}", "reset": "none"},
    "equations": {"enabled": True, "numeral": "arabic", "format": "({n})", "reset": "none"},
}


def para(text: str, style: str | None = None, extra: str = "") -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}{extra}<w:r><w:t>{text}</w:t></w:r></w:p>"


def eq_para(math: str, label: str | None = "(1)") -> str:
    tail = f"<w:r><w:t>{label}</w:t></w:r>" if label else ""
    return f"<w:p><m:oMathPara><m:oMath><m:r><m:t>{math}</m:t></m:r></m:oMath></m:oMathPara>{tail}</w:p>"


def drawing(cx: int, cy: int, rid: str = "rId10", svg_rid: str | None = None) -> str:
    svg = f'<a:extLst><a:ext><asvg:svgBlip r:embed="{svg_rid}"/></a:ext></a:extLst>' if svg_rid else ""
    return (
        f'<w:p><w:r><w:drawing><wp:inline><wp:extent cx="{cx}" cy="{cy}"/>'
        f'<a:graphic><a:graphicData><a:blip r:embed="{rid}">{svg}</a:blip></a:graphicData></a:graphic>'
        "</wp:inline></w:drawing></w:r></w:p>"
    )


def make_docx(
    path: Path,
    body: str = "",
    *,
    media: dict[str, bytes] | None = None,
    rels: str | None = None,
    content_types: str = CONTENT_TYPES,
    extra_members: list[tuple[str, bytes]] | None = None,
    document: str | None = None,
) -> Path:
    media = media or {}
    if rels is None:
        rels = "".join(
            f'<Relationship Id="rId{10 + i}" Type="x/image" Target="media/{name}"/>' for i, name in enumerate(media)
        )
    rels_xml = (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>"
    )
    doc = document if document is not None else f'<?xml version="1.0"?><w:document {NS}><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("word/document.xml", doc)
        zf.writestr("word/_rels/document.xml.rels", rels_xml)
        for name, data in media.items():
            zf.writestr(f"word/media/{name}", data)
        for name, data in extra_members or []:
            zf.writestr(name, data)
    return path


def expect(**kw: Any) -> DocxExpectations:
    kw.setdefault("numbering", NUMBERING)
    kw.setdefault("report", {})
    return DocxExpectations(**kw)


def codes(result: DocxValidation) -> list[str]:
    return [f.code for f in result.findings]


def run(tmp_path: Path, body: str, **kw: Any) -> DocxValidation:
    exp_kw = {k: kw.pop(k) for k in list(kw) if k in DocxExpectations.__dataclass_fields__}
    return validate_docx(make_docx(tmp_path / "t.docx", body, **kw), expect(**exp_kw))


def has(result: DocxValidation, code: str) -> bool:
    return f"L2W-{code[0]}-{code[2:]}" in codes(result) if code[1] == "-" else code in codes(result)


CLEAN_BODY = (
    para("I. Introduction", "Heading1")
    + para("Some text with a link.", "BodyText")
    + para("A. Detail", "Heading2")
    + para("II. Methods", "Heading1")
    + para("Fig. 1. First", "ImageCaption")
    + para("Fig. 2. Second", "ImageCaption")
    + para("TABLE I", "TableCaption")
    + para("TABLE II", "TableCaption")
    + eq_para("x=1", "(1)")
    + eq_para("y=2", "(2)")
    + para("[1] Ref.", "Bibliography")
)
CLEAN_REPORT = {
    "sequences": {
        "figures": [{"n": 1, "text": "Fig. 1."}, {"n": 2, "text": "Fig. 2."}],
        "tables": [{"n": 1, "text": "TABLE I"}, {"n": 2, "text": "TABLE II"}],
        "equations": [{"n": 1, "text": "(1)"}, {"n": 2, "text": "(2)"}],
        "sections": [
            {"level": 1, "n": 1, "text": "I.", "title": "Introduction"},
            {"level": 2, "n": 1, "text": "A.", "title": "Detail"},
            {"level": 1, "n": 2, "text": "II.", "title": "Methods"},
        ],
    },
    "math": {"literal_ampersands": 0},
}


# --- clean package -----------------------------------------------------------------------------


def test_clean_package_has_no_findings_and_full_metrics(tmp_path: Path) -> None:
    result = run(tmp_path, CLEAN_BODY, report=CLEAN_REPORT, bibliography_entries=1, citations_expected=True)
    assert result.findings == []
    assert result.ok
    m = result.metrics
    assert m["headings"] == 3 and m["bibliography_paragraphs"] == 1 and m["omml_display"] == 2
    assert m["numbering"]["figures"] == {"numbers": [1, 2], "texts": ["Fig. 1.", "Fig. 2."]}
    assert m["numbering"]["tables"]["numbers"] == [1, 2]
    assert m["numbering"]["tables"]["texts"] == ["TABLE I", "TABLE II"]
    assert m["numbering"]["equations"]["numbers"] == [1, 2]
    assert m["numbering"]["sections"]["numbers"] == [[1, 1], [2, 1], [1, 2]]
    assert "word/document.xml" in m["part_hashes"] and "word/_rels/document.xml.rels" in m["part_hashes"]


def test_table_separator_newline_is_tolerated(tmp_path: Path) -> None:
    numbering = {**NUMBERING, "tables": {**NUMBERING["tables"], "format": "TABLE {n}\n"}}
    body = '<w:p><w:pPr><w:pStyle w:val="TableCaption"/></w:pPr><w:r><w:t>TABLE I</w:t></w:r><w:r><w:br/></w:r><w:r><w:t>CAP</w:t></w:r></w:p>'
    result = run(tmp_path, body, numbering=numbering)
    assert result.metrics["numbering"]["tables"]["numbers"] == [1]
    assert result.findings == []


# --- OPC / XML ----------------------------------------------------------------------------------


def test_not_a_zip(tmp_path: Path) -> None:
    bad = tmp_path / "bad.docx"
    bad.write_text("nope")
    result = validate_docx(bad, expect())
    assert codes(result) == ["L2W-E-DOCX-001"] and not result.ok
    assert result.metrics["parts"] == 0


def test_missing_required_parts_and_duplicates(tmp_path: Path) -> None:
    p = tmp_path / "x.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("a.txt", "1")
        with pytest.warns(UserWarning):
            zf.writestr("a.txt", "2")
    result = validate_docx(p, expect())
    msgs = [f.message for f in result.findings if f.code == "L2W-E-DOCX-001"]
    assert any("[Content_Types].xml" in m for m in msgs)
    assert any("word/document.xml" in m for m in msgs)
    assert any("duplicate" in m and "a.txt" in m for m in msgs)


def test_malformed_xml_part(tmp_path: Path) -> None:
    result = run(tmp_path, CLEAN_BODY, extra_members=[("word/styles.xml", b"<w:styles")])
    found = [f for f in result.findings if f.code == "L2W-E-DOCX-002"]
    assert len(found) == 1 and "word/styles.xml" in found[0].message


def test_malformed_document_xml_does_not_raise(tmp_path: Path) -> None:
    result = run(tmp_path, "", document="<w:document")
    assert "L2W-E-DOCX-002" in codes(result)


def test_broken_relationship(tmp_path: Path) -> None:
    rels = '<Relationship Id="rId1" Type="x/image" Target="media/gone.png"/>'
    result = run(tmp_path, CLEAN_BODY, rels=rels)
    found = [f for f in result.findings if f.code == "L2W-E-DOCX-003"]
    assert len(found) == 1 and "word/media/gone.png" in found[0].message


def test_external_and_absolute_relationships(tmp_path: Path) -> None:
    rels = (
        '<Relationship Id="rId1" Type="x/hyperlink" Target="http://example.org/x" TargetMode="External"/>'
        '<Relationship Id="rId2" Type="x/styles" Target="/word/styles.xml"/>'
    )
    ok = run(tmp_path, CLEAN_BODY, rels=rels, extra_members=[("word/styles.xml", b"<s/>")])
    assert "L2W-E-DOCX-003" not in codes(ok)
    missing = run(tmp_path, CLEAN_BODY, rels=rels)
    assert "L2W-E-DOCX-003" in codes(missing)


def test_missing_content_type(tmp_path: Path) -> None:
    result = run(tmp_path, CLEAN_BODY, extra_members=[("word/odd.zzz", b"x")])
    found = [f for f in result.findings if f.code == "L2W-W-DOCX-004"]
    assert len(found) == 1 and "word/odd.zzz" in found[0].message
    assert result.ok


# --- bookmarks, links, raw TeX ------------------------------------------------------------------


def bookmark(name: str, i: int = 0) -> str:
    return f'<w:bookmarkStart w:id="{i}" w:name="{name}"/><w:bookmarkEnd w:id="{i}"/>'


def link(anchor: str) -> str:
    return f'<w:hyperlink w:anchor="{anchor}"><w:r><w:t>x</w:t></w:r></w:hyperlink>'


def test_links_resolve_and_metrics(tmp_path: Path) -> None:
    body = (
        f'<w:p>{bookmark("a")}{link("a")}<w:hyperlink r:id="rId5"><w:r><w:t>e</w:t></w:r></w:hyperlink></w:p>'
    )
    result = run(tmp_path, body, numbering={})
    assert result.findings == []
    assert result.metrics["bookmarks"] == 1
    assert result.metrics["internal_hyperlinks"] == 1 and result.metrics["external_hyperlinks"] == 1


def test_missing_bookmark(tmp_path: Path) -> None:
    result = run(tmp_path, f"<w:p>{bookmark('a')}{link('nope')}</w:p>", numbering={})
    found = [f for f in result.findings if f.code == "L2W-E-REF-010"]
    assert len(found) == 1 and "nope" in found[0].message


def test_duplicate_bookmark(tmp_path: Path) -> None:
    result = run(tmp_path, f"<w:p>{bookmark('a', 1)}{bookmark('a', 2)}</w:p>", numbering={})
    assert "L2W-E-REF-012" in codes(result)


def test_question_marks(tmp_path: Path) -> None:
    result = run(tmp_path, para("see Fig. ??"), numbering={})
    assert codes(result) == ["L2W-E-REF-011"]


@pytest.mark.parametrize("cmd", [r"\cite{a}", r"\citep{a}", r"\ref{x}", r"\eqref{x}", r"\autoref{x}", r"\cref{x}",
                                 r"\label{x}", r"\input{f}", r"\include{f}", r"\begin{x}", r"\end{x}"])
def test_raw_tex_in_text(tmp_path: Path, cmd: str) -> None:
    result = run(tmp_path, para(f"hello {cmd} world"), numbering={})
    found = [f for f in result.findings if f.code == "L2W-E-TEX-010"]
    assert found and cmd.split("{")[0] in found[0].message and "hello" in found[0].message


def test_raw_tex_in_math_text(tmp_path: Path) -> None:
    result = run(tmp_path, eq_para(r"\ref{a}", None), numbering={})
    assert "L2W-E-TEX-010" in codes(result)


def test_other_tex_command_is_warning(tmp_path: Path) -> None:
    result = run(tmp_path, para(r"a \foo b"), numbering={})
    assert codes(result) == ["L2W-W-TEX-011"] and result.ok


@pytest.mark.parametrize("text", ["as shown (smith2020?)", "see [?]", "see [@smith2020]"])
def test_citation_placeholder(tmp_path: Path, text: str) -> None:
    result = run(tmp_path, para(text), numbering={})
    assert codes(result) == ["L2W-E-CITE-010"]


# --- numbering ----------------------------------------------------------------------------------


def test_duplicate_figure_number(tmp_path: Path) -> None:
    body = para("Fig. 1. A", "ImageCaption") + para("Fig. 1. B", "ImageCaption")
    result = run(tmp_path, body)
    assert "L2W-E-NUM-010" in codes(result)
    assert "L2W-E-NUM-011" in codes(result)


def test_decreasing_table_number(tmp_path: Path) -> None:
    body = para("TABLE II", "TableCaption") + para("TABLE I", "TableCaption")
    result = run(tmp_path, body)
    assert codes(result) == ["L2W-E-NUM-011"]


def test_reset_skips_monotonic_checks(tmp_path: Path) -> None:
    numbering = {**NUMBERING, "figures": {**NUMBERING["figures"], "reset": "section"}}
    body = para("Fig. 1. A", "ImageCaption") + para("Fig. 1. B", "ImageCaption")
    assert run(tmp_path, body, numbering=numbering).findings == []


def test_sequence_differs_from_report(tmp_path: Path) -> None:
    body = para("Fig. 1. A", "ImageCaption") + para("Fig. 3. B", "ImageCaption")
    report = {"sequences": {"figures": [{"n": 1}, {"n": 2}]}}
    result = run(tmp_path, body, report=report)
    found = [f for f in result.findings if f.code == "L2W-E-NUM-012"]
    assert len(found) == 1 and "[1, 3]" in found[0].message and "[1, 2]" in found[0].message


def test_equation_label_count_differs_from_report(tmp_path: Path) -> None:
    body = eq_para("x", "(1)") + eq_para("y", None)
    report = {"sequences": {"equations": [{"n": 1}, {"n": 2}]}}
    assert codes(run(tmp_path, body, report=report)) == ["L2W-E-NUM-012"]


def test_section_sequence_mismatch(tmp_path: Path) -> None:
    body = para("I. A", "Heading1") + para("III. B", "Heading1")
    report = {"sequences": {"sections": [{"level": 1, "n": 1}, {"level": 1, "n": 2}]}}
    assert "L2W-E-NUM-012" in codes(run(tmp_path, body, report=report))


def test_duplicate_section_number(tmp_path: Path) -> None:
    body = para("I. A", "Heading1") + para("I. B", "Heading1")
    assert "L2W-E-NUM-010" in codes(run(tmp_path, body))


def test_unnumbered_heading_ok_and_violation(tmp_path: Path) -> None:
    report = {"unnumbered_headings": [{"level": 1, "title": "Abstract"}]}
    ok = run(tmp_path, para("Abstract", "Heading1") + para("I. Intro", "Heading1"), report=report)
    assert ok.findings == []
    bad = run(tmp_path, para("I. Abstract", "Heading1"), report=report)
    found = [f for f in bad.findings if f.code == "L2W-E-NUM-013"]
    assert len(found) == 1 and "Abstract" in found[0].message


def test_disabled_kind_is_skipped(tmp_path: Path) -> None:
    numbering = {**NUMBERING, "figures": {**NUMBERING["figures"], "enabled": False}}
    body = para("Fig. 1. A", "ImageCaption") + para("Fig. 1. B", "ImageCaption")
    result = run(tmp_path, body, numbering=numbering)
    assert result.findings == [] and "figures" not in result.metrics["numbering"]


# --- math ---------------------------------------------------------------------------------------


def eqarr(*rows: str) -> str:
    inner = "".join(f"<m:e><m:r><m:t>{r}</m:t></m:r></m:e>" for r in rows)
    return f"<w:p><m:oMathPara><m:oMath><m:eqArr>{inner}</m:eqArr></m:oMath></m:oMathPara></w:p>"


def test_eqarr_ampersand_leak(tmp_path: Path) -> None:
    result = run(tmp_path, eqarr("a&amp;=b", "c=d"), numbering={}, report={"math": {"literal_ampersands": 0}})
    msgs = [f.message for f in result.findings if f.code == "L2W-E-MATH-001"]
    assert len(msgs) == 2 and any("a&=b" in m for m in msgs)
    assert result.metrics["eqarr"] == 1


def test_clean_eqarr_passes(tmp_path: Path) -> None:
    result = run(tmp_path, eqarr("a=b", "c=d"), numbering={}, report={"math": {"literal_ampersands": 0}})
    assert result.findings == []


def test_unexpected_ampersands(tmp_path: Path) -> None:
    body = eq_para("a&amp;b", None)
    bad = run(tmp_path, body, numbering={}, report={"math": {"literal_ampersands": 0}})
    assert any("found 1, expected 0" in f.message for f in bad.findings if f.code == "L2W-E-MATH-001")
    good = run(tmp_path, body, numbering={}, report={"math": {"literal_ampersands": 1}})
    assert good.findings == []
    unchecked = run(tmp_path, body, numbering={}, report={})
    assert unchecked.findings == []


def test_raw_environment_in_math(tmp_path: Path) -> None:
    result = run(tmp_path, eq_para(r"\begin{foo} x \end{foo}", None), numbering={})
    assert "L2W-E-MATH-002" in codes(result)
    assert "L2W-E-TEX-010" in codes(result)


def test_inline_math_metric(tmp_path: Path) -> None:
    body = "<w:p><m:oMath><m:r><m:t>x</m:t></m:r></m:oMath></w:p>" + eq_para("y", None)
    m = run(tmp_path, body, numbering={}).metrics
    assert m["omml_inline"] == 1 and m["omml_display"] == 1


# --- figures ------------------------------------------------------------------------------------

PNG_A = b"\x89PNG-A"
PNG_B = b"\x89PNG-B"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inches(x: float) -> int:
    return round(x * 914400)


def test_figures_ok_different_intrinsic_scale_same_cx(tmp_path: Path) -> None:
    body = drawing(inches(3.0), inches(1.5), "rId10") + drawing(inches(3.0), inches(3.0), "rId11")
    figs = [
        FigureExpectation("a", sha(PNG_A), 2.0, 0.5, None),
        FigureExpectation("b", sha(PNG_B), 1.0, 0.5, None),
    ]
    result = run(tmp_path, body, media={"a.png": PNG_A, "b.png": PNG_B}, figures=figs, numbering={})
    assert result.findings == []
    rows = result.metrics["figures"]
    assert [r["id"] for r in rows] == ["a", "b"]
    assert rows[0]["implied_text_width_in"] == 6.0 and rows[0]["aspect"] == 2.0
    assert result.metrics["drawings"] == 2 and result.metrics["media"] == 2


def test_figure_svg_blip_matches_hash(tmp_path: Path) -> None:
    svg = b"<svg/>"
    body = drawing(inches(3), inches(3), "rId10", svg_rid="rId11")
    figs = [FigureExpectation("s", sha(svg), 1.0, 0.5, None)]
    result = run(tmp_path, body, media={"a.png": PNG_A, "a.svg": svg}, figures=figs, numbering={})
    assert result.findings == [] and result.metrics["svg_drawings"] == 1


def test_figure_asset_missing(tmp_path: Path) -> None:
    body = drawing(inches(3), inches(3), "rId10")
    figs = [FigureExpectation("gone", sha(b"other"), None, None, None)]
    result = run(tmp_path, body, media={"a.png": PNG_A}, figures=figs, numbering={})
    found = [f for f in result.findings if f.code == "L2W-E-FIG-011"]
    assert len(found) == 1 and "gone" in found[0].message and "missing" in found[0].message


def test_figure_count_mismatch(tmp_path: Path) -> None:
    body = drawing(inches(3), inches(3), "rId10")
    figs = [FigureExpectation("a", None, None, None, None), FigureExpectation("b", None, None, None, None)]
    result = run(tmp_path, body, media={"a.png": PNG_A}, figures=figs, numbering={})
    assert "L2W-E-FIG-010" in codes(result)


def test_figure_aspect_mismatch(tmp_path: Path) -> None:
    body = drawing(inches(3), inches(1), "rId10")
    figs = [FigureExpectation("a", sha(PNG_A), 1.0, 0.5, None)]
    result = run(tmp_path, body, media={"a.png": PNG_A}, figures=figs, numbering={})
    assert codes(result) == ["L2W-E-FIG-012"]


def test_same_fraction_different_cx_fails(tmp_path: Path) -> None:
    body = drawing(inches(3.0), inches(3.0), "rId10") + drawing(inches(4.5), inches(4.5), "rId11")
    figs = [
        FigureExpectation("a", sha(PNG_A), 1.0, 0.98, None, "0.98\\linewidth"),
        FigureExpectation("b", sha(PNG_B), 1.0, 0.98, None, "0.98\\linewidth"),
    ]
    result = run(tmp_path, body, media={"a.png": PNG_A, "b.png": PNG_B}, figures=figs, numbering={})
    found = [f for f in result.findings if f.code == "L2W-E-FIG-013"]
    assert found and "median" in found[0].message


def test_three_figures_one_outlier_named(tmp_path: Path) -> None:
    media = {"a.png": b"1", "b.png": b"2", "c.png": b"3"}
    body = drawing(inches(3), inches(3), "rId10") + drawing(inches(3), inches(3), "rId11") + drawing(
        inches(5), inches(5), "rId12"
    )
    figs = [FigureExpectation(n, sha(media[f"{n}.png"]), 1.0, 0.5, None) for n in "abc"]
    result = run(tmp_path, body, media=media, figures=figs, numbering={})
    found = [f for f in result.findings if f.code == "L2W-E-FIG-013"]
    assert len(found) == 1 and "'c'" in found[0].message


def test_text_width_inches_absolute_check(tmp_path: Path) -> None:
    body = drawing(inches(3.0), inches(3.0), "rId10")
    figs = [FigureExpectation("a", sha(PNG_A), 1.0, 0.5, None)]
    ok = run(tmp_path, body, media={"a.png": PNG_A}, figures=figs, numbering={}, text_width_inches=6.0)
    assert ok.findings == []
    bad = run(tmp_path, body, media={"a.png": PNG_A}, figures=figs, numbering={}, text_width_inches=7.0)
    assert codes(bad) == ["L2W-E-FIG-013"]


def test_absolute_width_inches(tmp_path: Path) -> None:
    body = drawing(inches(3.0), inches(3.0), "rId10")
    media = {"a.png": PNG_A}
    ok = run(tmp_path, body, media=media, numbering={},
             figures=[FigureExpectation("a", sha(PNG_A), 1.0, None, 3.0)])
    assert ok.findings == []
    bad = run(tmp_path, body, media=media, numbering={},
              figures=[FigureExpectation("a", sha(PNG_A), 1.0, None, 2.0, "2in")])
    assert codes(bad) == ["L2W-E-FIG-013"]


# --- bibliography -------------------------------------------------------------------------------


def test_bibliography_count_and_missing(tmp_path: Path) -> None:
    body = para("[1] A", "Bibliography") + para("[2] B", "Bibliography")
    assert codes(run(tmp_path, body, numbering={}, bibliography_entries=3)) == ["L2W-E-BIB-010"]
    assert run(tmp_path, body, numbering={}, bibliography_entries=2).findings == []
    none = run(tmp_path, para("x"), numbering={}, citations_expected=True)
    assert codes(none) == ["L2W-E-BIB-011"]
    zero = run(tmp_path, para("x"), numbering={}, citations_expected=True, bibliography_entries=0)
    assert zero.findings == []


# --- Pandoc smoke -------------------------------------------------------------------------------

needs_pandoc = pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")

STRUCTURAL = {"L2W-E-DOCX-001", "L2W-E-DOCX-002", "L2W-E-DOCX-003"}


def _pandoc(tmp_path: Path, source: str) -> Path:
    (tmp_path / "in.tex").write_text(source, encoding="utf-8")
    subprocess.run(
        ["pandoc", "-f", "latex", "-t", "docx", "-o", "out.docx", "in.tex"], cwd=tmp_path, check=True, timeout=120
    )
    return tmp_path / "out.docx"


@needs_pandoc
def test_pandoc_smoke_plain(tmp_path: Path) -> None:
    out = _pandoc(tmp_path, "\\section{Intro}\nHello \\emph{world} $x^2$.\n\n\\begin{equation}a=b\\end{equation}\n")
    result = validate_docx(out, expect(numbering={}))
    assert not STRUCTURAL & set(codes(result)), result.findings
    assert result.metrics["headings"] == 1
    assert result.metrics["omml_display"] >= 1 and result.metrics["omml_inline"] >= 1
    assert "word/styles.xml" in result.metrics["part_hashes"]


@needs_pandoc
def test_pandoc_smoke_links_and_image(tmp_path: Path) -> None:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
    )
    (tmp_path / "p.png").write_bytes(png)
    src = (
        "\\section{Intro}\\label{s:i}\nSee Section~\\ref{s:i}.\n\n"
        "\\begin{figure}\\includegraphics[width=3in]{p.png}\\caption{Cap}\\end{figure}\n"
    )
    out = _pandoc(tmp_path, src)
    fig = FigureExpectation("p", hashlib.sha256(png).hexdigest(), 1.0, None, 3.0)
    result = validate_docx(out, expect(numbering={}, figures=[fig]))
    assert not STRUCTURAL & set(codes(result)), result.findings
    assert "L2W-E-FIG-011" not in codes(result), result.findings
    assert result.metrics["drawings"] == 1


# --- two-paragraph equation shape (Pandoc DOCX writer) -------------------------------------------


def eq_two(math: str, label: str | None, style: str = "Equation") -> str:
    body = (
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
        f"<m:oMathPara><m:oMath><m:r><m:t>{math}</m:t></m:r></m:oMath></m:oMathPara></w:p>"
    )
    if label:
        body += para(label, "EquationNumber")
    return body


def test_equation_number_paragraph_style(tmp_path: Path) -> None:
    body = eq_two("x", "(1)") + eq_two("y", "(2)")
    report = {"sequences": {"equations": [{"n": 1}, {"n": 2}]}}
    result = run(tmp_path, body, report=report)
    assert result.findings == []
    assert result.metrics["numbering"]["equations"] == {"numbers": [1, 2], "texts": ["(1)", "(2)"]}


def test_unnumbered_equation_has_no_label(tmp_path: Path) -> None:
    body = eq_two("x", "(1)") + eq_two("y", None) + para("Plain text (7)")
    result = run(tmp_path, body, report={"sequences": {"equations": [{"n": 1}]}})
    assert result.findings == []
    assert result.metrics["numbering"]["equations"]["numbers"] == [1]


def test_equation_number_duplicate_detected(tmp_path: Path) -> None:
    result = run(tmp_path, eq_two("x", "(1)") + eq_two("y", "(1)"))
    assert "L2W-E-NUM-010" in codes(result)


def test_equation_label_in_next_plain_paragraph_fallback(tmp_path: Path) -> None:
    body = eq_two("x", None) + para("(1)")
    result = run(tmp_path, body)
    assert result.metrics["numbering"]["equations"]["numbers"] == [1]

