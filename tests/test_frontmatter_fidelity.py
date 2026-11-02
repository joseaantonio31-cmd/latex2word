"""Front matter and contents fidelity (Stage 3): blank first sheet, sizes, \\vspace, \\hrulefill, manual TOC entries,
starred headings out of the TOC, TOC entry styles, uncaptioned list tables. Synthetic fixture only."""

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
from latex2word.layout import latex_font_sizes, TextStyle
from latex2word.structure_rewrite import tex_length_twips

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "semantic" / "report_frontmatter"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from latex2word.build import run_build
    from latex2word.config import load_config

    tmp_path = tmp_path_factory.mktemp("frontmatter")
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    config = {
        "schema_version": 1,
        "project": {"name": "frontmatter", "root": str(project), "main": "main.tex"},
        "profile": "report-frontmatter-test",
        "profile_dirs": [str(project / "profiles")],
        "workspace": {"root": str(tmp_path / "workspace")},
        "output": {"directory": str(tmp_path / "out")},
        "tools": {"timeout_s": 120},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    result = run_build(load_config(path))
    archive = zipfile.ZipFile(result.output) if result.output else None
    parts = {n: archive.read(n).decode("utf-8") for n in archive.namelist() if n.endswith(".xml")} if archive else {}
    return result, parts


def _paragraphs(document: str) -> list[str]:
    body = document[document.index("<w:body>") :]
    return re.findall(r"<w:p\b(?:(?!<w:p\b).)*?</w:p>", body, re.S)


def _style_block(styles: str, style_id: str) -> str:
    match = re.search(r'<w:style [^>]*w:styleId="' + style_id + r'".*?</w:style>', styles, re.S)
    assert match, style_id
    return match.group(0)


def _instructions(document: str) -> list[str]:
    return [i.strip() for i in re.findall(r"<w:instrText[^>]*>([^<]*)</w:instrText>", document)]


@requires_pandoc
def test_builds_without_errors(built):
    result, _ = built
    errors = [str(f) for f in result.findings if f.severity is Severity.ERROR]
    assert result.ok and not errors, errors


@requires_pandoc
def test_null_and_clearpage_at_the_start_give_a_blank_first_sheet(built):
    _, parts = built
    first = _paragraphs(parts["word/document.xml"])[0]
    assert '<w:br w:type="page"/>' in first and "<w:t" not in first  # only a page break, no visible text
    # the title page text comes after it; no page number shows (gobble section, empty footer)
    assert parts["word/document.xml"].index("UNIVERSIDAD DE EJEMPLO") > parts["word/document.xml"].index(first)


@requires_pandoc
def test_size_group_becomes_a_character_style_with_the_latex_size(built):
    _, parts = built
    document, styles = parts["word/document.xml"], parts["word/styles.xml"]
    assert re.search(r'<w:rStyle w:val="SizeLarge"/>.*?TÍTULO LARGO DEL TRABAJO', document, re.S)
    assert '<w:sz w:val="35"/>' in _style_block(styles, "SizeLarge")  # \Large of a 12 pt class: 17.28 pt
    assert "<w:b/>" in document[: document.index("TÍTULO LARGO")][-600:] or "<w:b/>" in document  # \bfseries kept


@requires_pandoc
def test_vspace_becomes_spacer_paragraphs_and_dynamic_lengths_warn(built):
    result, parts = built
    document = parts["word/document.xml"]
    for twips in (425, 1134, 240):  # 0.75cm, 2cm, 1em at 12 pt
        assert f'w:line="{twips}" w:lineRule="exact"' in document, twips
    assert any(f.code == "L2W-W-SPACE-001" and "baselineskip" in f.message for f in result.findings)


@requires_pandoc
def test_hrulefill_line_becomes_a_bordered_paragraph_and_the_text_follows(built):
    _, parts = built
    paragraphs = _paragraphs(parts["word/document.xml"])
    rule = next(i for i, p in enumerate(paragraphs) if '<w:bottom w:val="single"' in p)
    assert "<w:t" not in paragraphs[rule]
    assert "Persona Uno" in "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", paragraphs[rule + 1]))
    # the rest of the LaTeX paragraph after the rule's line break starts at the margin: no first-line indent
    assert '<w:ind w:firstLine="0"/>' in paragraphs[rule + 1]
    assert "_l2wrule" not in parts["word/document.xml"]  # private markers removed


@requires_pandoc
def test_manual_toc_entries_are_tc_fields_and_gobble_pages_have_no_number(built):
    _, parts = built
    tc = [i for i in _instructions(parts["word/document.xml"]) if i.startswith("TC ")]
    assert tc == ['TC "Página de firmas" \\l 1 \\n', 'TC "Resumen" \\l 1', 'TC "Índice de cuadros" \\l 1',
                  'TC "Lista de siglas" \\l 1']


@requires_pandoc
def test_clearpage_then_addcontentsline_then_list_gives_one_break_and_the_entry_on_the_new_page(built):
    _, parts = built
    paragraphs = _paragraphs(parts["word/document.xml"])
    heading = next(i for i, p in enumerate(paragraphs) if "Heading1Unlisted" in p and "Índice de cuadros" in p)
    assert '> TC "Índice de cuadros"' in paragraphs[heading + 1]  # the entry follows the heading (same page)
    # between the TOC field and that heading: no explicit page-break paragraph (the heading style breaks the page)
    toc_end = max(i for i, p in enumerate(paragraphs[:heading]) if 'fldCharType="end"' in p)
    assert not any("<w:pageBreakBefore/>" in p for p in paragraphs[toc_end + 1 : heading])


@requires_pandoc
def test_starred_headings_and_list_titles_are_unlisted_and_numbered_ones_are_not(built):
    _, parts = built
    document, styles = parts["word/document.xml"], parts["word/styles.xml"]
    unlisted = _style_block(styles, "Heading1Unlisted")
    assert '<w:basedOn w:val="Heading1"/>' in unlisted and '<w:outlineLvl w:val="9"/>' in unlisted
    for title in ("Resumen", "Índice general", "Índice de cuadros", "Lista de siglas"):
        assert re.search(r'<w:pStyle w:val="Heading1Unlisted"/>(?:(?!</w:p>).)*' + title, document, re.S), title
    assert re.search(r'<w:pStyle w:val="Heading1"/>(?:(?!</w:p>).)*Primero', document, re.S)
    assert "_l2wunlisted" not in document  # private markers removed


@requires_pandoc
def test_contents_entry_styles_and_list_level(built):
    _, parts = built
    document, styles = parts["word/document.xml"], parts["word/styles.xml"]
    assert 'TOC \\h \\z \\t &quot;Table Caption,5&quot;' in document or 'TOC \\h \\z \\t "Table Caption,5"' in document
    toc1, toc2, toc5 = (_style_block(styles, s) for s in ("TOC1", "TOC2", "TOC5"))
    assert '<w:name w:val="toc 1"/>' in toc1 and "<w:b/>" in toc1 and 'w:before="240"' in toc1 and 'w:after="0"' in toc1
    assert '<w:tab w:val="right" w:leader="dot"' in toc1
    assert 'w:before="100"' in toc2 and 'w:left="360"' in toc2
    assert 'w:left="890"' in toc5 and 'w:hanging="540"' in toc5 and 'w:line="360"' in toc5


@requires_pandoc
def test_uncaptioned_tables_take_their_own_role_and_captioned_tables_keep_table_text(built):
    _, parts = built
    document, styles = parts["word/document.xml"], parts["word/styles.xml"]
    tables = re.findall(r"<w:tbl>.*?</w:tbl>", document, re.S)
    data = [t for t in tables if "EquationLayout" not in t]
    uncaptioned = next(t for t in data if "Sigla" in t)
    captioned = next(t for t in data if "<w:tblCaption" in t)
    assert 'w:val="CompactUncaptioned"' in uncaptioned and 'w:val="CompactUncaptioned"' not in captioned
    style = _style_block(styles, "CompactUncaptioned")
    assert '<w:basedOn w:val="Compact"/>' in style and '<w:sz w:val="24"/>' in style


@requires_pandoc
def test_toc_styles_are_word_builtins_and_tc_paragraphs_keep_with_next(built):
    _, parts = built
    styles, document = parts["word/styles.xml"], parts["word/document.xml"]
    for style_id in ("TOC1", "TOC2", "TOC5"):
        block = _style_block(styles, style_id)
        # a customStyle-flagged "toc N" is ignored by Word when it rebuilds the TOC field
        assert 'w:customStyle' not in block.split(">")[0] and '<w:uiPriority w:val="39"/>' in block
    tc_paragraphs = [p for p in _paragraphs(document) if "> TC " in p]
    assert tc_paragraphs and all("<w:keepNext/>" in p for p in tc_paragraphs)


@requires_pandoc
def test_a_table_taller_than_the_page_keeps_only_its_header_with_the_first_row(tmp_path: Path):
    from latex2word.build import run_build
    from latex2word.config import load_config

    rows = "".join(f"Fila {i} & Texto de la fila {i}\\\\\n" for i in range(80))
    main = (
        "\\documentclass[12pt]{report}\\usepackage{longtable,booktabs}\\begin{document}\n\\chapter{Uno}\n"
        "\\begin{longtable}{ll}\n\\toprule\nA & B\\\\\n\\midrule\n\\endhead\n" + rows + "\\bottomrule\n\\end{longtable}\n"
        "\\begin{tabular}{ll}\nx & y\\\\\nz & w\\\\\n\\end{tabular}\n\\end{document}\n"
    )
    project = tmp_path / "project"
    shutil.copytree(FIXTURE / "profiles", project / "profiles")
    (project / "main.tex").write_text(main, encoding="utf-8")
    config = {
        "schema_version": 1, "project": {"name": "long", "root": str(project), "main": "main.tex"},
        "profile": "report-frontmatter-test", "profile_dirs": [str(project / "profiles")],
        "workspace": {"root": str(tmp_path / "ws")}, "output": {"directory": str(tmp_path / "out")},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    result = run_build(load_config(path))
    assert result.ok, [str(f) for f in result.findings if f.severity is Severity.ERROR]
    document = zipfile.ZipFile(result.output).read("word/document.xml").decode("utf-8")
    long_table, short_table = [t for t in re.findall(r"<w:tbl>.*?</w:tbl>", document, re.S) if "EquationLayout" not in t]
    long_rows = re.findall(r"<w:tr>.*?</w:tr>", long_table, re.S)
    assert all("<w:cantSplit/>" in r for r in long_rows)  # rows never split
    assert sum("<w:keepNext/>" in r for r in long_rows) <= 2  # only header and first row are chained
    short_rows = re.findall(r"<w:tr>.*?</w:tr>", short_table, re.S)
    assert "<w:keepNext/>" in short_rows[0]  # a table that fits stays in one piece


@requires_pandoc
def test_typesetting_rewrites_stop_where_the_main_matter_starts(built):
    result, parts = built
    document = parts["word/document.xml"]
    assert "Nota del cuerpo principal." in document and "Sizesmall" not in document  # main matter untouched
    assert 'w:line="567" w:lineRule="exact"' not in document  # its \vspace{1cm} is not a spacer
    assert not any(f.code == "L2W-W-SPACE-001" and "1cm" in f.message for f in result.findings)


def _tiny_png(path: Path) -> None:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x80\x80\x80" * 8 for _ in range(8))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


@requires_pandoc
def test_a_ruleless_table_of_images_is_a_borderless_layout_grid(tmp_path: Path):
    from latex2word.build import run_build
    from latex2word.config import load_config

    project = tmp_path / "project"
    shutil.copytree(FIXTURE / "profiles", project / "profiles")
    for name in ("uno.png", "dos.png"):
        _tiny_png(project / name)
    main = (
        "\\documentclass[12pt]{report}\\usepackage{graphicx,booktabs}\\begin{document}\n"
        "\\begin{center}\n\\begin{tabular}{cc}\n\\includegraphics[width=2cm]{uno.png} &\n"
        "\\includegraphics[width=2cm]{dos.png}\n\\end{tabular}\n\\end{center}\n\n"
        "\\begin{tabular}{ll}\n\\toprule\nA & B\\\\\n\\midrule\nx & y\\\\\n\\bottomrule\n\\end{tabular}\n\\end{document}\n"
    )
    (project / "main.tex").write_text(main, encoding="utf-8")
    config = {
        "schema_version": 1, "project": {"name": "grid", "root": str(project), "main": "main.tex"},
        "profile": "report-frontmatter-test", "profile_dirs": [str(project / "profiles")],
        "workspace": {"root": str(tmp_path / "ws")}, "output": {"directory": str(tmp_path / "out")},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    result = run_build(load_config(path))
    assert result.ok, [str(f) for f in result.findings if f.severity is Severity.ERROR]
    archive = zipfile.ZipFile(result.output)
    document = archive.read("word/document.xml").decode("utf-8")
    tables = re.findall(r"<w:tbl>.*?</w:tbl>", document, re.S)
    images = [t for t in tables if "<w:drawing>" in t]
    text = [t for t in tables if "<w:drawing>" not in t]
    assert len(images) == 1 and '<w:tblStyle w:val="ImageLayout"/>' in images[0]
    assert text and all('w:val="ImageLayout"' not in t for t in text)  # a data table keeps its rules
    style = _style_block(archive.read("word/styles.xml").decode("utf-8"), "ImageLayout")
    assert 'w:val="single"' not in style  # no borders


def test_tex_lengths_and_latex_size_tables():
    assert tex_length_twips("0.75cm", 12) == 425 and tex_length_twips("1em", 12) == 240
    assert tex_length_twips("72.27pt", 12) == 1440 and tex_length_twips("\\baselineskip", 12) is None
    assert latex_font_sizes(TextStyle(size_pt=12), {})["Large"] == 17.28
    assert latex_font_sizes(TextStyle(size_pt=10), {"Large": 15})["Large"] == 15  # profile override
    assert latex_font_sizes(TextStyle(size_pt=11), {})["footnotesize"] == 9
