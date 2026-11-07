"""Report-class documents: constructs found in a full-scale thesis conversion (2026-11).

Unit tests for the staged-copy rewrites (column types, longtable continuation
blocks, siunitx, structure markers) and one end-to-end build of a synthetic
Spanish report fixture (``tests/fixtures/semantic/report_structure``) that
checks the DOCX: tables with custom column types, multi-label and capitalised
cross-references, four heading levels, lettered appendix numbering, siunitx in
text and math, bibliography placement before the appendix, TOC fields, page
breaks, centred paragraphs, figure notes outside the figure table, and node
names in table cells.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT
from pandockit import requires_pandoc
from projectkit import make_project

from latex2word.columntypes import (
    ColumnType,
    expand_spec,
    plan_column_type_rewrites,
    plan_longtable_rewrites,
    plan_multicolumn_rewrites,
    plan_partial_rule_rewrites,
    simplify_cell_spec,
)
from latex2word.diagnostics import Severity
from latex2word.rewrite import drop_contained
from latex2word.siunitx import (
    LANGUAGE_WORDS,
    NBSP,
    Settings,
    UnitParser,
    _Context,
    apply_options,
    babel_language,
    expand_command,
    plan_siunitx_rewrites,
)
from latex2word.source_graph import SourceLocation, build_source_graph
from latex2word.structure_rewrite import contents_titles, plan_structure_rewrites

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "semantic" / "report_structure"
WHERE = SourceLocation("main.tex", 1)


def graph_of(tmp_path: Path, main: str, files: dict[str, str] | None = None):
    root = make_project(tmp_path, {"main.tex": main, **(files or {})})
    return build_source_graph(root, root / "main.tex")


def apply(graph, edits) -> str:
    text = graph.files["main.tex"].text
    for edit in sorted(edits, key=lambda e: e.start, reverse=True):
        text = text[: edit.start] + edit.replacement + text[edit.end :]
    return text


# ----------------------------------------------------------------------------- column types


def test_custom_column_types_expand_with_arguments_and_recursively():
    types = {
        "P": ColumnType("P", 1, ">{\\raggedright\\arraybackslash}p{#1}", WHERE),
        "Y": ColumnType("Y", 0, ">{\\raggedright\\arraybackslash}X", WHERE),
        "Z": ColumnType("Z", 0, "P{1cm}", WHERE),
    }
    assert expand_spec("@{}P{2.2cm}YY@{}", types) == (
        "@{}>{\\raggedright\\arraybackslash}p{2.2cm}>{\\raggedright\\arraybackslash}X"
        ">{\\raggedright\\arraybackslash}X@{}"
    )
    assert expand_spec("Z", types) == ">{\\raggedright\\arraybackslash}p{1cm}"
    # arguments of standard specifiers are never mistaken for custom types
    assert expand_spec("p{P}>{\\bfseries}l", types) == "p{P}>{\\bfseries}l"
    loop = {"A": ColumnType("A", 0, "A", WHERE)}
    with pytest.raises(ValueError):
        expand_spec("A", loop)


def test_column_type_plan_rewrites_longtable_and_tabularx_specs(tmp_path: Path):
    main = (
        "\\documentclass{report}\\usepackage{array,tabularx,longtable}\n"
        "\\newcolumntype{P}[1]{>{\\raggedright\\arraybackslash}p{#1}}\n"
        "\\begin{document}\n"
        "\\begin{longtable}{@{}P{2cm}l@{}}\na & b\\\\\n\\end{longtable}\n"
        "\\begin{tabularx}{\\textwidth}{P{1cm}X}\na & b\\\\\n\\end{tabularx}\n"
        "\\begin{tabular}{ll}\na & b\\\\\n\\end{tabular}\n\\end{document}\n"
    )
    graph = graph_of(tmp_path, main)
    plan = plan_column_type_rewrites(graph)
    assert [e.subject for e in plan.edits] == ["longtable", "tabularx"]
    text = apply(graph, plan.edits)
    assert "{@{}>{\\raggedright\\arraybackslash}p{2cm}l@{}}" in text
    assert "{\\textwidth}{>{\\raggedright\\arraybackslash}p{1cm}X}" in text


def test_longtable_continuation_head_and_foot_are_removed(tmp_path: Path):
    main = (
        "\\documentclass{report}\\usepackage{longtable}\\begin{document}\n"
        "\\begin{longtable}{ll}\n\\caption{C}\\\\\nH1 & H2\\\\\n\\endfirsthead\nR1 & R2\\\\\n\\endhead\n"
        "\\multicolumn{2}{r}{Continua}\\\\\n\\endfoot\n\\hline\n\\endlastfoot\nx & y\\\\\n\\end{longtable}\n"
        "\\begin{longtable}{ll}\nH & H\\\\\n\\endhead\nx & y\\\\\n\\end{longtable}\n\\end{document}\n"
    )
    graph = graph_of(tmp_path, main)
    text = apply(graph, plan_longtable_rewrites(graph))
    assert "H1 & H2" in text and "\\endfirsthead" in text
    assert "R1" not in text and "Continua" not in text
    assert "H & H\\\\\n\\endhead" in text  # a head without \endfirsthead is the only head: kept
    # the final foot moves after the last row (Pandoc would put it at the top of the body)
    assert "\\endlastfoot" not in text and text.index("x & y") < text.index("\\hline")


def test_final_foot_row_with_a_decorated_multicolumn_moves_after_the_last_row(tmp_path: Path):
    main = (
        "\\documentclass{report}\\usepackage{longtable,booktabs}\\begin{document}\n"
        "\\begin{longtable}{@{}ll@{}}\n\\caption{C}\\\\\nH1 & H2\\\\\n\\endfirsthead\n"
        "\\multicolumn{2}{@{}p{\\textwidth}@{}}{Total}\\\\\n\\bottomrule\n\\endlastfoot\nx & y\\\\\n\\end{longtable}\n"
        "\\end{document}\n"
    )
    graph = graph_of(tmp_path, main)
    text = apply(graph, drop_contained([*plan_longtable_rewrites(graph), *plan_multicolumn_rewrites(graph)]))
    # above the last rule the full-width cell is a table row: it stays in the table, decorations dropped
    assert "\\multicolumn{2}{p{\\textwidth}}{Total}" in text and "l2wnote" not in text
    assert text.index("x & y") < text.index("Total") < text.index("\\bottomrule") < text.index("\\end{longtable}")


def test_final_foot_note_under_the_last_rule_becomes_a_note_block_below_the_table(tmp_path: Path):
    main = (
        "\\documentclass{report}\\usepackage{longtable,booktabs}\\begin{document}\n"
        "\\begin{longtable}{@{}lll@{}}\n\\caption{C}\\\\\nH1 & H2 & H3\\\\\n\\endfirsthead\n"
        "\\midrule\n\\multicolumn{3}{r@{}}{\\footnotesize Continúa}\\\\\n\\endfoot\n\\bottomrule\n"
        "\\multicolumn{3}{@{}p{\\textwidth}@{}}{%\n  \\vspace{4pt}\\footnotesize\\textit{Nota.} Primera nota.}\\\\\n"
        "\\multicolumn{3}{l}{\\raggedright Segunda nota \\ref{x}.}\n"
        "\\endlastfoot\nx & y & z\\\\\n\\end{longtable}\n"
        "Texto siguiente.\n\\end{document}\n"
    )
    graph = graph_of(tmp_path, main)
    text = apply(graph, drop_contained([*plan_longtable_rewrites(graph, 12.0), *plan_multicolumn_rewrites(graph)]))
    table = text[text.index("\\begin{longtable}") : text.index("\\end{longtable}")]
    after = text[text.index("\\end{longtable}") : text.index("Texto siguiente.")]
    # the bottom rule stays the table's last line; no note text and no multicolumn note row inside the table
    assert table.rstrip().endswith("\\bottomrule") and "Nota" not in table and "Segunda" not in table
    assert "Continúa" not in table  # per-page continuation foot dropped as before
    # each note is a note block below the table: its leading skip (4 pt = 80 twips) and declarations give the shape
    assert "\\begin{l2wnote-footnotesize-j-n-80-o}{\\footnotesize\\textit{Nota.} Primera nota.\\par}" in after
    assert "\\begin{l2wnote-normalsize-l-n-0-o}{\\raggedright Segunda nota \\ref{x}.\\par}" in after
    assert after.index("Primera") < after.index("Segunda")


def test_partial_width_foot_row_under_the_rule_stays_in_the_table(tmp_path: Path):
    main = (
        "\\begin{longtable}{lll}\nH1 & H2 & H3\\\\\n\\endfirsthead\n\\bottomrule\n"
        "\\multicolumn{2}{l}{Parcial} & 3\\\\\n\\endlastfoot\nx & y & z\\\\\n\\end{longtable}\n"
    )
    graph = graph_of(tmp_path, main)
    text = apply(graph, plan_longtable_rewrites(graph))
    assert "l2wnote" not in text and text.index("x & y & z") < text.index("Parcial") < text.index("\\end{longtable}")


def test_partial_rules_are_removed_from_tables_only(tmp_path: Path):
    main = (
        "\\documentclass{article}\\usepackage{booktabs,tabularx}\\begin{document}\n"
        "\\begin{tabular}{llll}\\toprule\n& \\multicolumn{3}{c}{Grupo} \\\\\n"
        "\\cmidrule(lr){2-2}\\cmidrule[0.4pt](l){3-4} \\cmidrule{1-1}\n"
        "A & B & C & D \\\\\n\\cline{2-3}\nE & F & G & H \\\\\n\\bottomrule\\end{tabular}\n"
        "\\begin{tabularx}{\\textwidth}{lX}\\cmidrule(r){1-2}\nx & y \\\\\n\\end{tabularx}\n"
        "Fuera de una tabla: \\cline{1-2} queda intacto.\n"
        "\\end{document}\n"
    )
    graph = graph_of(tmp_path, main)
    edits = plan_partial_rule_rewrites(graph)
    assert [e.subject for e in edits] == ["cmidrule", "cmidrule", "cmidrule", "cline", "cmidrule"]
    assert all(e.kind == "table-rule" and e.replacement == "" for e in edits)
    text = apply(graph, edits)
    assert "\\cmidrule" not in text and "(lr)" not in text and "{2-2}" not in text and "{3-4}" not in text
    assert "\nA & B & C & D \\\\\n\nE & F" in text  # rows untouched; only the rules are gone
    assert "\\toprule" in text and "\\bottomrule" in text and "\\multicolumn{3}{c}{Grupo}" in text
    assert "Fuera de una tabla: \\cline{1-2} queda intacto." in text


def test_malformed_partial_rule_is_left_for_pandoc(tmp_path: Path):
    main = "\\begin{tabular}{ll}a & b \\\\\n\\cmidrule(lr x & y \\\\\n\\end{tabular}\n"
    assert plan_partial_rule_rewrites(graph_of(tmp_path, main)) == []


def test_partial_rules_are_dropped_from_a_moved_longtable_foot(tmp_path: Path):
    main = (
        "\\documentclass{report}\\usepackage{longtable,booktabs}\\begin{document}\n"
        "\\begin{longtable}{lll}\n\\caption{C}\\\\\nH1 & H2 & H3\\\\\n\\endfirsthead\n"
        "\\cmidrule(lr){2-3}\nTotal & 1 & 2\\\\\n\\endlastfoot\nx & y & z\\\\\n\\end{longtable}\n\\end{document}\n"
    )
    graph = graph_of(tmp_path, main)
    text = apply(graph, drop_contained([*plan_longtable_rewrites(graph), *plan_partial_rule_rewrites(graph)]))
    assert "\\cmidrule" not in text and "{2-3}" not in text
    assert text.index("x & y & z") < text.index("Total & 1 & 2") < text.index("\\end{longtable}")


@pytest.mark.parametrize(
    ("spec", "simple"),
    [("@{}p{\\textwidth}@{}", "p{\\textwidth}"), ("r@{}", "r"), ("|c|", "c"), (">{\\bfseries}l", "l"),
     ("P{2cm}", ">{\\raggedright\\arraybackslash}p{2cm}"), ("ll", None)],
)
def test_multicolumn_specs_reduce_to_one_column_type(spec: str, simple: str | None):
    types = {"P": ColumnType("P", 1, ">{\\raggedright\\arraybackslash}p{#1}", WHERE)}
    result = simplify_cell_spec(spec, types)
    # a custom type expands to its decorated form and is then reduced to its single type
    assert result == ("p{2cm}" if spec == "P{2cm}" else simple)


def test_cell_decorations_are_stripped_only_from_tables_with_multicolumn_rows(tmp_path: Path):
    """Pandoc silently drops \\multicolumn rows of a table whose preamble has >{..}/<{..} decorations."""
    main = (
        "\\documentclass{report}\\usepackage{array}\\begin{document}\n"
        "\\begin{tabular}{@{}>{\\centering}p{2cm}l@{}}\na & b\\\\\n\\multicolumn{2}{l}{Nota}\\\\\n\\end{tabular}\n"
        "\\begin{tabular}{>{\\centering}p{2cm}l}\na & b\\\\\n\\end{tabular}\n\\end{document}\n"
    )
    graph = graph_of(tmp_path, main)
    text = apply(graph, plan_column_type_rewrites(graph).edits)
    assert "\\begin{tabular}{@{}p{2cm}l@{}}" in text  # with a \multicolumn row: decorations removed
    assert "\\begin{tabular}{>{\\centering}p{2cm}l}" in text  # without: untouched


# ----------------------------------------------------------------------------- siunitx


@pytest.fixture
def ctx() -> _Context:
    settings = replace(Settings(), words=dict(LANGUAGE_WORDS["spanish"]))
    settings = apply_options(settings, "per-mode=symbol, range-phrase={--}, range-units=single")
    return _Context(settings, UnitParser({"dBm": "dBm", "sample": "Sa", "Vpp": "V_{pp}"}))


@pytest.mark.parametrize(
    ("name", "args", "text", "math"),
    [
        ("SI", ["5", "\\dBm"], f"5{NBSP}dBm", "5\\,\\mathrm{dBm}"),
        ("SI", ["10", "\\giga\\sample\\per\\second"], f"10{NBSP}GSa/s", "10\\,\\mathrm{GSa}/\\mathrm{s}"),
        ("SI", ["-3", "\\decibel"], f"\u22123{NBSP}dB", "-3\\,\\mathrm{dB}"),
        ("SI", ["3", "\\Vpp"], f"3{NBSP}V\\textsubscript{{pp}}", "3\\,{\\mathrm{V}}_{\\mathrm{pp}}"),
        ("SI", ["10", "\\percent"], f"10{NBSP}\\%", "10\\,\\%"),
        ("SIrange", ["300", "900", "\\mega\\hertz"], f"300\u2013900{NBSP}MHz", "300\\text{\u2013}900\\,\\mathrm{MHz}"),
        ("SIlist", ["1;5;10", "\\nano\\second"], f"1{NBSP}ns, 5{NBSP}ns y 10{NBSP}ns", None),
        ("num", ["1e-6"], f"1{NBSP}\u00d7{NBSP}10\\textsuperscript{{\u22126}}", "1\\times 10^{-6}"),
        ("SI", ["1.5 \\pm 0.2", "\\volt"], f"(1.5{NBSP}\u00b1{NBSP}0.2){NBSP}V", "\\left(1.5\\pm 0.2\\right)\\,\\mathrm{V}"),
        # no {}^{\circ}: an empty script base becomes a zero-width space in OMML
        ("SI", ["25", "\\celsius"], f"25{NBSP}\u00b0C", "25\\,\\mathrm{\u00b0C}"),
        ("ang", ["30"], "30\u00b0", "30^{\\circ}"),
    ],
)
def test_siunitx_expansion_in_text_and_math(ctx, name, args, text, math):
    assert expand_command(name, args, None, ctx, False) == text
    if math is not None:
        assert expand_command(name, args, None, ctx, True) == math


def test_siunitx_plan_honours_math_mode_text_groups_and_reports_unknown_units(tmp_path: Path):
    main = (
        "\\documentclass{report}\\usepackage[english]{babel}\\usepackage{siunitx}\n"
        "\\DeclareSIUnit{\\dBm}{dBm}\n\\begin{document}\n"
        "A \\SI{5}{\\dBm}; $P>\\SI{10}{\\dBm}$; $x\\text{ at \\SI{2}{\\volt}}$; \\SI{1}{\\unknownunit}.\n"
        "\\end{document}\n"
    )
    graph = graph_of(tmp_path, main)
    plan = plan_siunitx_rewrites(graph)
    text = apply(graph, plan.edits)
    assert f"A {{5{NBSP}dBm}};" in text
    assert "$P>10\\,\\mathrm{dBm}$" in text
    assert f"\\text{{ at {{2{NBSP}V}}}}" in text  # \text{} inside math is text again
    assert "\\SI{1}{\\unknownunit}" in text  # never guessed
    assert [f.code for f in plan.findings] == ["L2W-W-UNIT-001"]


def test_babel_main_language():
    assert babel_language("\\usepackage[english,spanish]{babel}", "\\usepackage[english,spanish]{babel}") == "spanish"
    assert babel_language("\\usepackage[main=french,english]{babel}", "\\usepackage[main=french,english]{babel}") == "french"
    assert babel_language("\\usepackage{amsmath}", "\\usepackage{amsmath}") is None


# ----------------------------------------------------------------------------- structure markers


def test_structure_markers_and_contents_titles(tmp_path: Path):
    main = (
        "\\documentclass{report}\\usepackage[spanish]{babel}\n"
        "\\addto\\captionsspanish{\\renewcommand{\\listfigurename}{Índice de gráficas}}\n"
        "\\begin{document}\n\\tableofcontents\n\\listoffigures\n\\clearpage\n\\chapter{A}\n"
        "{\\sloppy\\printbibliography[heading=bibintoc,title={Mis referencias}]}\n"
        "\\printbibliography\n\\appendix\n\\chapter{B}\n\\end{document}\n"
    )
    graph = graph_of(tmp_path, main)
    plan = plan_structure_rewrites(graph, place_bibliography=True)
    text = apply(graph, plan.edits)
    assert plan.contents == ["toc", "lof"] and plan.bibliography_title == "Mis referencias"
    for marker in ("l2wtoc", "l2wlof", "l2wpagebreak", "l2wrefs", "l2wappendix"):
        assert f"\\begin{{{marker}}}\\end{{{marker}}}" in text
    assert text.count("l2wrefs") == 2 and "\\printbibliography\n" in text  # only the first one is the place
    assert [f.code for f in plan.findings] == ["L2W-W-BIB-020"]
    titles = contents_titles(graph, "spanish")
    assert titles == {"toc": "Índice general", "lof": "Índice de gráficas", "lot": "Índice de cuadros"}
    without = plan_structure_rewrites(graph, place_bibliography=False)
    assert "l2wrefs" not in apply(graph, without.edits)


# ----------------------------------------------------------------------------- end to end


def _xml(path: Path, part: str) -> str:
    return zipfile.ZipFile(path).read(part).decode("utf-8")


def _text(path: Path) -> str:
    xml = re.sub(r"</w:p>", "\n", _xml(path, "word/document.xml"))
    return re.sub(r"<[^>]+>", "", xml).replace("&amp;", "&")


@requires_pandoc
def test_report_fixture_builds_with_faithful_structure(tmp_path: Path):
    from latex2word.build import run_build
    from latex2word.config import load_config

    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    data = {
        "schema_version": 1,
        "project": {"name": "report", "root": str(project), "main": "main.tex"},
        "profile": "report-es-test",
        "profile_dirs": [str(project / "profiles")],
        "workspace": {"root": str(tmp_path / "workspace")},
        "output": {"directory": str(tmp_path / "out")},
        "tools": {"timeout_s": 120},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    result = run_build(load_config(config_path))
    problems = [str(f) for f in result.findings if f.severity is Severity.ERROR]
    assert result.ok and not problems, problems

    text = _text(result.output)
    document = _xml(result.output, "word/document.xml")
    # headings: four levels, per-chapter numbering, lettered appendix
    for heading in ("Capítulo 1. Primero", "1.1 Alfa", "1.1.1 Beta", "1.1.1.1 Gamma", "Anexo A. Detalles", "A.1 Uno del anexo"):
        assert heading in text, heading
    # references: aliases, comma lists, \Cref capitalisation, appendix names
    assert "Ver sección 1.1.1.1, sección 1.1.1.1 y sección 1.1 y sección 1.1.1." in text
    assert "Ecuaciones ecuación (1.1) y ecuación (1.2). Cuadro 1.1 resume; el cuadro 1.2 y la figura 1.1." in text
    assert "Ver anexo A, sección A.1, ecuación (A.1) y Cuadro A.1." in text
    assert "??" not in text
    # tables with custom column types survive as Word tables; no continuation rows
    assert "Cuadro 1.1. Una tabla larga" in text and "Cuadro 1.2. Tabla ancha" in text
    assert "Nodo (cont.)" not in text and "Continúa en la página siguiente" not in text
    assert "FILTER_OUT" in text
    # a note in the final foot (decorated \multicolumn spec) survives, under the last row
    assert text.index("FILTER_OUT") < text.index("Nota final de la tabla larga") < text.index("Cuadro 1.2.")
    # siunitx: declared units in text, \percent escaped, units inside math converted to OMML
    assert f"5{NBSP}dBm" in text and f"10{NBSP}GSa/s" in text and f"300\u2013900{NBSP}MHz" in text
    assert f"\u22123{NBSP}dB" in text and f"10{NBSP}% de margen" in text
    math_text = "".join(re.findall(r"<m:t[^>]*>([^<]*)</m:t>", document))  # one run per letter in OMML
    assert "dBm" in math_text and "&gt;10" in math_text.replace(" ", "").replace(" ", "")
    assert "\\SI" not in text and "\\SI" not in math_text
    # bibliography: placed where \printbibliography was (before the appendix), with the document's title
    assert text.index("Referencias del documento") < text.index("Autora, Ana") < text.index("Anexo A. Detalles")
    # contents lists: TOC fields, titles from babel / the document, Word asked to update them
    instructions = re.findall(r"<w:instrText[^>]*>([^<]*)</w:instrText>", document)
    assert any(i.strip().startswith('TOC \\o "1-') for i in instructions)
    assert any('\\t "Image Caption,1"' in i for i in instructions) and any('\\t "Table Caption,1"' in i for i in instructions)
    assert "Índice general" in text and "Índice de gráficas" in text and "Índice de cuadros" in text
    settings = _xml(result.output, "word/settings.xml")
    assert '<w:updateFields w:val="true"/>' in settings and "<w:doNotExpandShiftReturn/>" in settings
    # page breaks, centred paragraphs, chapter style starting a new page
    # the two \clearpage before level-1 headings are absorbed by the heading style; the \newpage is kept
    assert document.count("<w:pageBreakBefore/>") == 1
    assert 'w:pStyle w:val="Centered"' in document
    assert re.search(r'w:styleId="Heading1".*?<w:pageBreakBefore/>', _xml(result.output, "word/styles.xml"), re.S)
    # the figure note follows the figure as a body paragraph, not as a table cell beside the image
    note = document.index("Elaboración propia")
    assert document.rfind("<w:tbl>", 0, note) <= document.rfind("</w:tbl>", 0, note)
