from __future__ import annotations

import hashlib
import stat
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from pandockit import PANDOC, requires_pandoc

from latex2word.errors import BuildError
from latex2word.layout import ReferenceDocxConfig
from latex2word.ooxml.namespaces import w
from latex2word.ooxml.package import OpcPackage, parse_xml, serialize_xml
from latex2word.ooxml.styles import apply_typography, describe_styles, style_ids_in
from latex2word.pandoc_adapter import PandocTool, locate_pandoc
from latex2word.profiles import Profile, load_profile
from latex2word.reference_doc import ReferenceDoc, build_reference_docx

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
UNUSED_TOOL = PandocTool("unused-pandoc", "3.12", "pandoc 3.12")

TEMPLATE_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    ),
    "word/_rels/document.xml.rels": (
        '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    ),
    "word/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}"><w:body/></w:document>',
    "word/styles.xml": (
        f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{W_NS}">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
        '<w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/><w:sz w:val="44"/></w:rPr></w:style>'
        "</w:styles>"
    ),
}


def make_template(path: Path, styles_xml: bytes | None = None) -> bytes:
    parts = {name: text.encode() for name, text in TEMPLATE_PARTS.items()}
    if styles_xml is not None:
        parts["word/styles.xml"] = styles_xml
    path.write_bytes(OpcPackage(parts).to_bytes())
    return path.read_bytes()


def external_profile(path: Path | None, base: str = "ieee-conference") -> Profile:
    return replace(load_profile(base), reference_docx=ReferenceDocxConfig("external", None), reference_docx_path=path)


def styles_of(docx: Path) -> object:
    with zipfile.ZipFile(docx) as archive:
        return parse_xml(archive.read("word/styles.xml"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- generated (real Pandoc)


@pytest.fixture(scope="module")
def pandoc_tool() -> PandocTool:
    if PANDOC is None:
        pytest.skip("pandoc is not installed")
    return locate_pandoc()


@requires_pandoc
def test_generated_reference_carries_page_geometry_and_profile_styles(tmp_path, pandoc_tool):
    profile = load_profile("ieee-conference")
    result = build_reference_docx(profile, pandoc_tool, tmp_path / "ref")
    assert result.path == tmp_path / "ref" / "reference.docx"
    assert (result.mode, result.pandoc_version) == ("generated", pandoc_tool.version)
    assert result.sha256 == sha256_file(result.path)
    assert {"Equation", "EquationNumber", "IndexTerms", "AuthorBlock", "EquationLayout"} <= set(result.created_styles)
    with zipfile.ZipFile(result.path) as archive:
        assert archive.testzip() is None
        document = parse_xml(archive.read("word/document.xml"))
    body = document.find(w("body"))
    assert [child.tag for child in body] == [w("sectPr")]
    size, margin, columns = (body.find(f"{w('sectPr')}/{w(name)}") for name in ("pgSz", "pgMar", "cols"))
    assert (size.get(w("w")), size.get(w("h"))) == ("12240", "15840")
    assert [margin.get(w(side)) for side in ("top", "bottom", "left", "right")] == ["1080", "1440", "900", "900"]
    assert (columns.get(w("num")), columns.get(w("space"))) == ("2", "360")
    described = describe_styles(styles_of(result.path), ["Heading1", "Title"])
    assert described["Heading1"]["font"] == "Times New Roman" and described["Title"]["size_pt"] == 24.0


@requires_pandoc
def test_generated_reference_records_the_pandoc_default_it_started_from(tmp_path, pandoc_tool):
    default = subprocess.run(
        [pandoc_tool.path, "--print-default-data-file", "reference.docx"], capture_output=True, check=True, stdin=subprocess.DEVNULL
    ).stdout
    result = build_reference_docx(load_profile("generic"), pandoc_tool, tmp_path)
    assert result.source_sha256 == hashlib.sha256(default).hexdigest()
    assert result.to_dict() == {
        "path": str(result.path), "sha256": result.sha256, "mode": "generated", "source_sha256": result.source_sha256,
        "pandoc_version": pandoc_tool.version, "created_styles": list(result.created_styles),
    }


@requires_pandoc
def test_generated_reference_is_byte_reproducible(tmp_path, pandoc_tool, monkeypatch):
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    profile = load_profile("ieee-conference")
    first = build_reference_docx(profile, pandoc_tool, tmp_path / "one")
    second = build_reference_docx(profile, pandoc_tool, tmp_path / "two")
    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.created_styles == second.created_styles


@requires_pandoc
def test_core_properties_use_source_date_epoch(tmp_path, pandoc_tool, monkeypatch):
    def timestamps(directory: Path) -> list[str]:
        result = build_reference_docx(load_profile("generic"), pandoc_tool, directory)
        with zipfile.ZipFile(result.path) as archive:
            core = parse_xml(archive.read("docProps/core.xml"))
        return [e.text for e in core.iter() if isinstance(e.tag, str) and e.tag.endswith(("}created", "}modified"))]

    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    assert timestamps(tmp_path / "default") == ["1970-01-01T00:00:00Z"] * 2
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    assert timestamps(tmp_path / "epoch") == ["2023-11-14T22:13:20Z"] * 2


@requires_pandoc
def test_pandoc_output_inherits_geometry_and_typography_from_the_reference(tmp_path, pandoc_tool):
    reference = build_reference_docx(load_profile("ieee-conference"), pandoc_tool, tmp_path / "ref")
    source = tmp_path / "doc.md"
    source.write_text("---\ntitle: Example Title\n---\n\n# Introduction\n\nBody text.\n", encoding="utf-8")
    output = tmp_path / "out.docx"
    subprocess.run(
        [pandoc_tool.path, str(source), "-o", str(output), f"--reference-doc={reference.path}"],
        check=True, capture_output=True, stdin=subprocess.DEVNULL,
    )
    with zipfile.ZipFile(output) as archive:
        document = parse_xml(archive.read("word/document.xml"))
        styles = parse_xml(archive.read("word/styles.xml"))
    size = document.find(f".//{w('sectPr')}/{w('pgSz')}")
    margin = document.find(f".//{w('sectPr')}/{w('pgMar')}")
    assert (size.get(w("w")), size.get(w("h"))) == ("12240", "15840")
    assert [margin.get(w(side)) for side in ("top", "bottom", "left", "right")] == ["1080", "1440", "900", "900"]
    described = describe_styles(styles, ["Title", "Heading1", "Bibliography"])
    assert described["Title"]["size_pt"] == 24.0 and described["Title"]["alignment"] == "center"
    assert described["Heading1"]["font"] == "Times New Roman" and described["Heading1"]["caps"] == "small"
    assert described["Bibliography"]["size_pt"] == 8.0
    assert not [n for n in styles.iter(w("color")) if n.get(w("themeColor")) and n.getparent().getparent().get(w("styleId"), "").startswith("Heading")]


# --------------------------------------------------------------------------- generated (no Pandoc needed)


def test_a_missing_pandoc_binary_is_a_build_error(tmp_path):
    with pytest.raises(BuildError, match="L2W-E-REFDOC-003"):
        build_reference_docx(load_profile("generic"), PandocTool(str(tmp_path / "no-such-pandoc"), "3.12", ""), tmp_path / "ref")
    assert not (tmp_path / "ref" / "reference.docx").exists()


def test_a_failing_pandoc_reports_its_message(tmp_path):
    failing = PandocTool(sys.executable, "0", "")  # `python --print-default-data-file reference.docx` exits non-zero
    with pytest.raises(BuildError, match="L2W-E-REFDOC-003"):
        build_reference_docx(load_profile("generic"), failing, tmp_path)


def test_a_profile_without_layout_is_refused(tmp_path):
    with pytest.raises(BuildError, match="L2W-E-REFDOC-004"):
        build_reference_docx(replace(load_profile("generic"), layout=None), UNUSED_TOOL, tmp_path)


# --------------------------------------------------------------------------- external


def test_external_template_gets_missing_styles_and_keeps_existing_ones(tmp_path):
    template = tmp_path / "publisher.docx"
    original = make_template(template)
    template.chmod(stat.S_IREAD)
    mtime = template.stat().st_mtime_ns
    try:
        result = build_reference_docx(external_profile(template), UNUSED_TOOL, tmp_path / "work")
        assert (template.read_bytes(), template.stat().st_mtime_ns) == (original, mtime)
    finally:
        template.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert result.mode == "external" and result.source_sha256 == hashlib.sha256(original).hexdigest()
    assert result.sha256 == sha256_file(result.path) != result.source_sha256
    assert {"Heading2", "Equation", "TableCaption", "Table", "EquationLayout"} <= set(result.created_styles)
    assert "Heading1" not in result.created_styles and "Normal" not in result.created_styles
    styles = styles_of(result.path)
    heading1 = [s for s in styles.iterfind(w("style")) if s.get(w("styleId")) == "Heading1"]
    assert len(heading1) == 1
    assert heading1[0].find(f"{w('rPr')}/{w('sz')}").get(w("val")) == "44"  # the template's own, not the profile's 20
    assert heading1[0].find(f"{w('rPr')}/{w('rFonts')}").get(w("ascii")) == "Arial"
    assert style_ids_in(styles) >= {"Normal", "Heading1", "Heading2", "Equation", "Table"}
    with zipfile.ZipFile(result.path) as archive:
        assert set(archive.namelist()) == set(TEMPLATE_PARTS)


def test_external_template_that_has_everything_is_copied_byte_for_byte(tmp_path):
    complete = parse_xml(TEMPLATE_PARTS["word/styles.xml"].encode())
    layout = load_profile("ieee-conference").layout
    apply_typography(complete, layout)
    template = tmp_path / "complete.docx"
    original = make_template(template, serialize_xml(complete))
    result = build_reference_docx(external_profile(template), UNUSED_TOOL, tmp_path / "work")
    assert result.created_styles == ()
    assert result.path.read_bytes() == original and result.sha256 == result.source_sha256


def test_external_output_is_reproducible(tmp_path):
    template = tmp_path / "publisher.docx"
    make_template(template)
    first = build_reference_docx(external_profile(template), UNUSED_TOOL, tmp_path / "a")
    second = build_reference_docx(external_profile(template), UNUSED_TOOL, tmp_path / "b")
    assert first.sha256 == second.sha256


@pytest.mark.parametrize("kind", ["missing", "not-a-zip", "no-styles", "broken-styles"])
def test_unusable_external_templates_are_refused(tmp_path, kind):
    template = tmp_path / "bad.docx"
    if kind == "not-a-zip":
        template.write_bytes(b"this is not a zip archive")
    elif kind == "no-styles":
        template.write_bytes(OpcPackage({"word/document.xml": b"<x/>"}).to_bytes())
    elif kind == "broken-styles":
        make_template(template, b"<w:styles")
    with pytest.raises(BuildError, match="L2W-E-REFDOC-001"):
        build_reference_docx(external_profile(template), UNUSED_TOOL, tmp_path / "work")
    assert not (tmp_path / "work" / "reference.docx").exists()


def test_external_mode_without_a_path_is_refused(tmp_path):
    with pytest.raises(BuildError, match="L2W-E-REFDOC-002"):
        build_reference_docx(external_profile(None), UNUSED_TOOL, tmp_path)


def test_reference_doc_is_a_frozen_record():
    record = ReferenceDoc(Path("r.docx"), "a", "generated", "b", "3.12", ("X",))
    with pytest.raises(AttributeError):
        record.sha256 = "c"  # type: ignore[misc]
    assert record.to_dict()["created_styles"] == ["X"]
