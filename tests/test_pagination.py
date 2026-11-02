"""Word sections, page-number formats and PAGE fields from \\pagenumbering / \\setcounter{page} (synthetic fixtures)."""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT
from pandockit import requires_pandoc

from latex2word.diagnostics import Severity
from latex2word.errors import ProfileError

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "semantic" / "report_pagination"
PROFILE = "report-pagination-test"


def _build(tmp_path: Path, *, main: str | None = None, pagination: dict | None = None):
    from latex2word.build import run_build
    from latex2word.config import load_config

    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    if main is not None:
        (project / "main.tex").write_text(main, encoding="utf-8")
    if pagination is not None:
        path = project / "profiles" / f"{PROFILE}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["layout"]["pagination"] = pagination
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config = {
        "schema_version": 1,
        "project": {"name": "pagination", "root": str(project), "main": "main.tex"},
        "profile": PROFILE,
        "profile_dirs": [str(project / "profiles")],
        "workspace": {"root": str(tmp_path / "workspace")},
        "output": {"directory": str(tmp_path / "out")},
        "tools": {"timeout_s": 120},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return run_build(load_config(path))


def _parts(result) -> dict[str, str]:
    archive = zipfile.ZipFile(result.output)
    return {name: archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith((".xml", ".rels"))}


def _sections(document: str) -> list[str]:
    return re.findall(r"<w:sectPr\b.*?</w:sectPr>", document, re.S)


def _ok(result) -> None:
    errors = [str(f) for f in result.findings if f.severity is Severity.ERROR]
    assert result.ok and not errors, errors


@requires_pandoc
def test_pagenumbering_creates_sections_with_formats_restarts_and_page_fields(tmp_path: Path):
    result = _build(tmp_path)
    _ok(result)
    parts = _parts(result)
    document = parts["word/document.xml"]
    sections = _sections(document)
    # gobble (title page) | roman from i (front matter) | arabic restarting at 1 (main matter + appendix)
    assert len(sections) == 3
    assert '<w:pgNumType w:fmt="decimal" w:start="1"/>' in sections[0]
    assert '<w:pgNumType w:fmt="lowerRoman" w:start="1"/>' in sections[1]
    assert '<w:pgNumType w:fmt="decimal" w:start="1"/>' in sections[2]
    assert all("<w:pgSz" in s and "<w:pgMar" in s for s in sections)  # geometry copied into every section
    # footers: an empty one where the source asked for no number, the PAGE field elsewhere
    rels = parts["word/_rels/document.xml.rels"]
    ids = dict(re.findall(r'Id="([^"]+)"[^>]*Target="(footer[^"]+)"', rels))
    targets = [ids[i] for s in sections for i in re.findall(r'<w:footerReference w:type="default" r:id="([^"]+)"', s)]
    assert targets == ["footer_l2w_empty.xml", "footer_l2w_page.xml", "footer_l2w_page.xml"]
    page_footer = parts["word/footer_l2w_page.xml"]
    assert re.search(r"<w:instrText[^>]*> PAGE </w:instrText>", page_footer)
    assert '<w:jc w:val="center"/>' in page_footer
    assert "PAGE" not in parts["word/footer_l2w_empty.xml"]
    types = parts["[Content_Types].xml"]
    assert 'PartName="/word/footer_l2w_page.xml"' in types and "wordprocessingml.footer+xml" in types
    # no literal page numbers, no private markers left
    assert "_l2wpn_" not in document
    # TOC field marked dirty and Word asked to update fields
    assert re.search(r'<w:fldChar w:fldCharType="begin" w:dirty="true"/>', document)
    assert '<w:updateFields w:val="true"/>' in parts["word/settings.xml"]


@requires_pandoc
def test_no_blank_page_between_a_section_break_and_a_page_breaking_heading(tmp_path: Path):
    result = _build(tmp_path)
    _ok(result)
    document = _parts(result)["word/document.xml"]
    body = document[document.index("<w:body>") :]
    paragraphs = re.findall(r"<w:p\b.*?</w:p>", body, re.S)
    for index, p in enumerate(paragraphs):
        if "<w:sectPr" in p:
            # the \clearpage before \pagenumbering is absorbed (no page-break paragraph before the carrier)
            assert "<w:pageBreakBefore/>" not in paragraphs[index - 1]
            # the heading that opens the new section does not break the page a second time
            assert '<w:pageBreakBefore w:val="0"/>' in paragraphs[index + 1]


@requires_pandoc
def test_setcounter_page_and_order_relative_to_pagenumbering(tmp_path: Path):
    main = (
        "\\documentclass{report}\\begin{document}\n\\pagenumbering{roman}\\setcounter{page}{5}\n\\chapter*{A}\nTexto.\n"
        "\\clearpage\\setcounter{page}{9}\\pagenumbering{Roman}\n\\chapter*{B}\nTexto.\n"
        "\\clearpage\\setcounter{page}{3}\n\\chapter*{C}\nTexto.\n\\end{document}\n"
    )
    result = _build(tmp_path, main=main)
    _ok(result)
    sections = _sections(_parts(result)["word/document.xml"])
    assert len(sections) == 3
    assert '<w:pgNumType w:fmt="lowerRoman" w:start="5"/>' in sections[0]  # first marker: no section before it
    assert '<w:pgNumType w:fmt="upperRoman" w:start="1"/>' in sections[1]  # \pagenumbering resets after \setcounter
    assert '<w:pgNumType w:fmt="upperRoman" w:start="3"/>' in sections[2]  # \setcounter alone keeps the format


@requires_pandoc
def test_header_position_and_first_page_suppression(tmp_path: Path):
    result = _build(tmp_path, pagination={"page_numbers": "header-right", "follow_source": True,
                                          "suppress_first_page": True})
    _ok(result)
    parts = _parts(result)
    sections = _sections(parts["word/document.xml"])
    assert "word/header_l2w_page.xml" in parts and "footerReference" not in parts["word/document.xml"]
    assert '<w:jc w:val="right"/>' in parts["word/header_l2w_page.xml"]
    # only the first numbered section (roman) suppresses its first page
    assert "<w:titlePg/>" in sections[1] and '<w:headerReference w:type="first"' in sections[1]
    assert "<w:titlePg/>" not in sections[0] and "<w:titlePg/>" not in sections[2]


@requires_pandoc
def test_document_without_pagenumbering_gets_one_numbered_section(tmp_path: Path):
    main = "\\documentclass{report}\\begin{document}\n\\chapter{A}\nTexto.\n\\end{document}\n"
    result = _build(tmp_path, main=main, pagination={"page_numbers": "footer-right", "format": "upperRoman", "start": 4})
    _ok(result)
    sections = _sections(_parts(result)["word/document.xml"])
    assert len(sections) == 1 and '<w:pgNumType w:fmt="upperRoman" w:start="4"/>' in sections[0]


@requires_pandoc
def test_follow_source_false_ignores_pagenumbering(tmp_path: Path):
    result = _build(tmp_path, pagination={"page_numbers": "none", "follow_source": False})
    _ok(result)
    parts = _parts(result)
    assert len(_sections(parts["word/document.xml"])) == 1
    assert not any(name.startswith("word/footer") for name in parts)


def test_pagination_profile_values_are_schema_validated(tmp_path: Path):
    from latex2word.profiles import load_profile

    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "bad.yaml").write_text(
        "profile_schema_version: 1\nid: bad\nextends: generic\nlayout:\n  pagination:\n    page_numbers: margin\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileError):
        load_profile("bad", [directory])
    profile = load_profile("generic", [])
    assert profile.layout.pagination.page_numbers == "none" and profile.layout.pagination.follow_source


def test_layout_validator_requires_update_fields_with_toc(tmp_path: Path):
    """The validator reports a TOC field without w:updateFields (a Word re-save drops it)."""
    from latex2word.ooxml.package import OpcPackage
    from latex2word.ooxml.validation import LayoutValidation, _check_pagination
    from latex2word.profiles import load_profile

    profile = load_profile("generic", [])
    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = (
        f'<w:document xmlns:w="{w_ns}"><w:body><w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText> TOC \\o "1-3" </w:instrText></w:r></w:p><w:sectPr/></w:body></w:document>'
    ).encode()
    package = OpcPackage({"word/document.xml": document, "word/settings.xml": f'<w:settings xmlns:w="{w_ns}"/>'.encode()})
    from types import SimpleNamespace

    result = LayoutValidation()
    doc = package.xml("word/document.xml")
    _check_pagination(package, doc, doc.find(f"{{{w_ns}}}body"), SimpleNamespace(layout=profile.layout), result)
    codes = sorted(f.code for f in result.findings)
    assert codes == ["L2W-E-LAYOUT-093", "L2W-E-LAYOUT-094"]
