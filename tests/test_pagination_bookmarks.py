"""After a section break the next page starts with the next paragraph, even behind bookmark boundaries.

Pandoc 3.12 writes section bookmarks at body level: a labelled front-matter section closes (``bookmarkEnd``) and the
next one opens (``bookmarkStart``) between the section-break paragraph and the next heading. That heading must still get
``w:pageBreakBefore w:val="0"`` (the section break already starts the page).
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest
import yaml
from lxml import etree
from pandockit import requires_pandoc

from latex2word.build import run_build
from latex2word.config import load_config
from latex2word.layout import PaginationLayout
from latex2word.ooxml.namespaces import NS, w
from latex2word.ooxml.package import OpcPackage
from latex2word.ooxml.pagination import _next_paragraph, apply_pagination


def _body(inner: str) -> etree._Element:
    return etree.fromstring(f'<w:document xmlns:w="{NS["w"]}"><w:body>{inner}</w:body></w:document>').find(w("body"))


def _p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


START = '<w:bookmarkStart w:id="{0}" w:name="sec{0}"/>'
END = '<w:bookmarkEnd w:id="{0}"/>'
CARRIER = _p("carrier")


def _found(inner_after_carrier: str) -> str | None:
    body = _body(CARRIER + inner_after_carrier)
    found = _next_paragraph(body[0])
    return None if found is None else "".join(found.itertext())


@pytest.mark.parametrize(("between", "name"), [
    ("", "nothing"),
    (START.format(1), "bookmarkStart only"),
    (END.format(1), "bookmarkEnd only"),
    (END.format(1) + START.format(2), "bookmarkEnd + bookmarkStart (Pandoc 3.12)"),
    (START.format(1) + END.format(1), "bookmarkStart + bookmarkEnd"),
    (END.format(1) + END.format(2) + START.format(3) + START.format(4) + END.format(5), "several adjacent"),
])
def test_bookmark_boundaries_are_transparent(between: str, name: str):
    assert _found(between + _p("Introduction") + _p("later")) == "Introduction", name


@pytest.mark.parametrize("unrelated", [
    "<w:tbl/>",
    "<w:sdt><w:sdtContent>" + _p("in a content control") + "</w:sdtContent></w:sdt>",
    "<w:customXml/>",
])
def test_other_elements_are_not_skipped(unrelated: str):
    # a table or content control starts the page: no paragraph behind it may receive the override
    assert _found(END.format(1) + unrelated + _p("Introduction")) is None
    assert _found(unrelated + START.format(1) + _p("Introduction")) is None


def test_nothing_after_the_carrier():
    assert _found("") is None
    assert _found(END.format(1) + START.format(2)) is None


def _paginate(between: str) -> etree._Element:
    marker = '<w:p><w:bookmarkStart w:id="90" w:name="_l2wpn_decimal_1"/><w:bookmarkEnd w:id="90"/></w:p>'
    inner = (_p("Front matter") + marker + between + _p("Introduction")
             + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>')
    document = _body(inner).getparent()
    outcome = apply_pagination(OpcPackage({}), document, PaginationLayout(format="lowerRoman"))
    assert [s["format"] for s in outcome.sections] == ["lowerRoman", "decimal"]
    return document.find(w("body"))


def _page_break_before(paragraph: etree._Element) -> str | None:
    node = paragraph.find(f"{w('pPr')}/{w('pageBreakBefore')}")
    return None if node is None else node.get(w("val"))


@pytest.mark.parametrize("between", ["", END.format(1) + START.format(2)])
def test_the_heading_after_a_section_break_gets_page_break_before_0(between: str):
    body = _paginate(between)
    heading = next(p for p in body.iter(w("p")) if "".join(p.itertext()) == "Introduction")
    assert _page_break_before(heading) == "0"
    carrier = heading.getprevious()
    while carrier.tag != w("p"):
        carrier = carrier.getprevious()
    assert carrier.find(f"{w('pPr')}/{w('sectPr')}") is not None  # the marker became the section break


def test_a_table_after_a_section_break_is_left_alone():
    body = _paginate(END.format(1) + "<w:tbl/>")
    assert all(_page_break_before(p) is None for p in body.iter(w("p")))


@requires_pandoc
def test_a_labelled_front_matter_section_before_the_break(tmp_path: Path):
    # the shape that left the first chapter without the override (Pandoc 3.12); passes on every version
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tex").write_text(
        "\\documentclass{report}\n\\begin{document}\n\\pagenumbering{roman}\n\\chapter*{Abstract}\\label{abs}\n"
        "Front matter.\n\\clearpage\n\\pagenumbering{arabic}\n\\chapter{Introduction}\\label{ch:intro}\n"
        "Text, see Chapter~\\ref{ch:intro}.\n\\end{document}\n", encoding="utf-8")
    data = {"schema_version": 1, "project": {"name": "pg", "root": str(project), "main": "main.tex"},
            "profile": "generic", "workspace": {"root": str(tmp_path / "ws")}, "output": {"directory": str(tmp_path / "out")}}
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    result = run_build(load_config(tmp_path / "c.yaml"))
    assert result.ok, [str(f) for f in result.errors]
    body = etree.fromstring(zipfile.ZipFile(result.output).read("word/document.xml")).find(w("body"))
    carriers = [p for p in body.findall(w("p")) if p.find(f"{w('pPr')}/{w('sectPr')}") is not None]
    assert len(carriers) == 1
    heading = _next_paragraph(carriers[0])
    assert heading is not None and "Introduction" in "".join(heading.itertext())
    assert _page_break_before(heading) == "0"
    shutil.rmtree(tmp_path / "ws", ignore_errors=True)
