"""Note blocks of figures and tables (Stage 3.1): a paragraph-level formatted group attached to a float keeps
its source semantics as the Word style "Caption Note"; prose, captions and other size groups are untouched.
Synthetic fixture only."""

from __future__ import annotations

import re
import shutil
import struct
import zipfile
import zlib
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT
from pandockit import requires_pandoc

from latex2word.diagnostics import Severity
from latex2word.layout import TextStyle, latex_baselineskip

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "semantic" / "report_notes"


def _tiny_png(path: Path) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x40\x80\xc0" * 8 for _ in range(8))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


def _build(tmp_path: Path, profile_patch: dict | None = None):
    from latex2word.build import run_build
    from latex2word.config import load_config

    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    _tiny_png(project / "pic.png")
    if profile_patch:
        path = project / "profiles" / "report-notes-test.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["layout"]["typography"].update(profile_patch)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    config = {
        "schema_version": 1,
        "project": {"name": "notes", "root": str(project), "main": "main.tex"},
        "profile": "report-notes-test",
        "profile_dirs": [str(project / "profiles")],
        "workspace": {"root": str(tmp_path / "workspace")},
        "output": {"directory": str(tmp_path / "out")},
        "tools": {"timeout_s": 120},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    result = run_build(load_config(path))
    assert result.ok, [str(f) for f in result.findings if f.severity is Severity.ERROR]
    archive = zipfile.ZipFile(result.output)
    return result, {n: archive.read(n).decode("utf-8") for n in archive.namelist() if n.endswith(".xml")}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("notes"))


def _paragraphs(document: str) -> list[str]:
    body = document[document.index("<w:body>") :]
    return re.findall(r"<w:p\b(?:(?!<w:p\b).)*?</w:p>", body, re.S)


def _text(paragraph: str) -> str:
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", paragraph))


def _style(paragraph: str) -> str:
    match = re.search(r'<w:pStyle w:val="([^"]+)"', paragraph)
    return match.group(1) if match else ""


def _style_block(styles: str, style_id: str) -> str:
    match = re.search(r'<w:style [^>]*w:styleId="' + style_id + r'".*?</w:style>', styles, re.S)
    assert match, style_id
    return match.group(0)


def _notes(document: str) -> list[str]:
    return [p for p in _paragraphs(document) if _style(p) == "CaptionNote"]


@requires_pandoc
def test_exactly_the_float_notes_become_caption_notes(built):
    _, parts = built
    texts = [_text(p) for p in _notes(parts["word/document.xml"])]
    assert len(texts) == 4, texts
    assert texts[0].startswith("Remark. First synthetic note") and texts[0].endswith("a bold word.")
    assert texts[1].startswith("Remark. Second synthetic note")
    assert texts[2].startswith("Remark. Third synthetic note")
    assert texts[3] == "Literal note group with other declarations."


@requires_pandoc
def test_prose_and_size_groups_outside_floats_are_untouched(built):
    _, parts = built
    paragraphs = _paragraphs(parts["word/document.xml"])
    for needle in ("A remark in running prose", "A size group in running prose", "Opening paragraph", "Closing paragraph"):
        paragraph = next(p for p in paragraphs if needle in _text(p))
        assert _style(paragraph) in ("BodyText", "FirstParagraph"), needle
        assert "<w:sz " not in paragraph and "Size" not in paragraph, needle  # no size of its own


@requires_pandoc
def test_note_style_follows_the_source_declarations(built):
    _, parts = built
    style = _style_block(parts["word/styles.xml"], "CaptionNote")
    assert '<w:name w:val="Caption Note"/>' in style and '<w:basedOn w:val="BodyText"/>' in style
    assert '<w:sz w:val="20"/>' in style  # \footnotesize of a 12 pt class: 10 pt
    assert '<w:jc w:val="left"/>' in style  # \raggedright
    assert 'w:firstLine="0"' in style  # \raggedright sets \parindent to 0
    assert 'w:before="120"' in style and 'w:after="0"' in style  # \medskip, nothing after
    assert 'w:line="240" w:lineRule="atLeast"' in style  # \footnotesize baselineskip: 12 pt (single-spaced float)


@requires_pandoc
def test_a_note_with_other_declarations_gets_them_as_direct_formatting(built):
    _, parts = built
    literal = _notes(parts["word/document.xml"])[3]
    assert '<w:jc w:val="center"/>' in literal and 'w:firstLine="0"' in literal
    assert 'w:line="190"' in literal  # \scriptsize baselineskip 9.5 pt
    assert '<w:sz w:val="16"/>' in literal  # \scriptsize: 8 pt on every run


@requires_pandoc
def test_a_note_outside_floats_takes_the_document_line_stretch(built):
    _, parts = built
    notes = _notes(parts["word/document.xml"])
    # after a long table, not in a float: \footnotesize baselineskip 12 pt x (18 / 14.5) = 14.9 pt
    assert 'w:line="298" w:lineRule="atLeast"' in notes[2]
    assert 'w:line=' not in notes[0] and 'w:line=' not in notes[1]  # float notes: the style's single spacing


@requires_pandoc
def test_note_text_keeps_its_inline_formatting_and_stays_editable_outside_images_and_tables(built):
    _, parts = built
    document = parts["word/document.xml"]
    first = _notes(document)[0]
    assert re.search(r"<w:i/>.*?Remark\.", first, re.S)  # \textit{Remark.}
    assert re.search(r"<w:b/>.*?a bold word", first, re.S)
    assert "<w:drawing>" not in first and "_l2wnote" not in document
    body = document[document.index("<w:body>") :]
    for note in _notes(document):
        position = body.index(note)
        assert body.rfind("<w:tbl>", 0, position) <= body.rfind("</w:tbl>", 0, position)  # not inside a table


@requires_pandoc
def test_figure_caption_and_note_stay_together_and_the_float_gap_moves_below_the_note(built):
    _, parts = built
    paragraphs = _paragraphs(parts["word/document.xml"])
    first = next(i for i, p in enumerate(paragraphs) if _text(p).startswith("Remark. First"))
    caption = paragraphs[first - 1]
    assert _style(caption) == "ImageCaption"
    assert "<w:keepNext/>" in caption and 'w:after="0"' in caption
    assert 'w:after="200"' in paragraphs[first]  # the caption's 10 pt after now follows the note
    # the text after the float is an ordinary paragraph, as without a note block
    assert _style(paragraphs[first + 1]) == "BodyText" and _text(paragraphs[first + 1]).startswith("Text after")


@requires_pandoc
def test_profile_role_overrides_the_source_values(tmp_path: Path):
    _, parts = _build(tmp_path, {"note": {"size_pt": 9, "line_height": "11pt", "after_pt": 3}})
    style = _style_block(parts["word/styles.xml"], "CaptionNote")
    assert '<w:sz w:val="18"/>' in style and 'w:line="220"' in style and 'w:after="60"' in style
    assert '<w:jc w:val="left"/>' in style  # not set by the profile: still the source's


def test_note_macro_shapes():
    from latex2word.structure_rewrite import _group_shape, _skip_before

    def shape(body: str):
        skip = _skip_before(body, 0, len(body), 12)
        if skip is None or skip[1] >= len(body) or body[skip[1]] != "{":
            return None
        found = _group_shape(body, skip[1])
        return (found[:3], skip[0]) if found else None

    assert shape(r"\par\medskip{\raggedright\footnotesize\textit{N.} #1\par}") == (("footnotesize", "l", True), 120)
    assert shape(r"\vspace{4pt}{\small #1\par}") == (("small", "j", False), 80)
    assert shape(r"{\noindent\scriptsize #1\par}") == (("scriptsize", "j", True), 0)
    assert shape(r"{\raggedright #1\par}") is None  # no size: not a note
    assert shape(r"{\footnotesize #1}") is None  # does not end its paragraph
    assert shape(r"{\footnotesize\begin{tabular}{l}x\end{tabular}\par}") is None  # wraps a table


def test_latex_baselineskips():
    assert latex_baselineskip(TextStyle(size_pt=12), "footnotesize") == 12
    assert latex_baselineskip(TextStyle(size_pt=10), "small") == 11
    assert latex_baselineskip(TextStyle(size_pt=11), "normalsize") == 13.6
    assert latex_baselineskip(TextStyle(size_pt=12), "unknown") is None
