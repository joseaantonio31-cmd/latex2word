"""Pandoc 3.12 AST facts the latex2word filters are designed on (see docs/pandoc-ast-findings.md).

Each test records one observed behaviour of the real Pandoc LaTeX reader. If a
Pandoc upgrade changes one of them, the filter design must be re-examined.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pandockit import blocks_of, document, pandoc_json, requires_pandoc, text_of, walk

pytestmark = requires_pandoc


def attr_dict(attr: list) -> dict[str, str]:
    return dict(attr[2])


def test_numbered_section_has_label_as_identifier_and_no_number(tmp_path: Path):
    ast = pandoc_json(tmp_path, document("\\section{Intro}\\label{sec:intro}\n\\subsection{Sub}\\label{sec:sub}"))
    first, second = blocks_of(ast, "Header")
    assert first["c"][0] == 1 and first["c"][1][0] == "sec:intro" and first["c"][1][1] == []
    assert second["c"][0] == 2 and second["c"][1][0] == "sec:sub"
    assert "number" not in attr_dict(first["c"][1])


def test_starred_section_is_marked_unnumbered(tmp_path: Path):
    ast = pandoc_json(tmp_path, document("\\section*{Data Availability}\n\\section{Numbered}"))
    starred, numbered = blocks_of(ast, "Header")
    assert starred["c"][1][1] == ["unnumbered"]
    assert numbered["c"][1][1] == []


def test_figure_is_a_figure_block_with_label_caption_and_raw_width(tmp_path: Path):
    body = "\\begin{figure}\\includegraphics[width=0.98\\columnwidth]{x.png}\\caption{Cap}\\label{fig:a}\\end{figure}"
    (figure,) = blocks_of(pandoc_json(tmp_path, document(body)), "Figure")
    assert figure["c"][0][0] == "fig:a"
    assert text_of(figure["c"][1]) == "Cap"
    (image,) = [e for e in walk(figure["c"][2]) if e["t"] == "Image"]
    assert attr_dict(image["c"][0])["width"] == "0.98\\columnwidth"  # Pandoc does not understand \columnwidth


def test_linewidth_and_textwidth_become_percentages_but_columnwidth_does_not(tmp_path: Path):
    body = "\n\n".join(
        f"\\includegraphics[width={w}]{{x.png}}" for w in ("0.5\\linewidth", "0.5\\textwidth", "0.5\\columnwidth", ".5\\textwidth")
    )
    images = [e for e in walk(pandoc_json(tmp_path, document(body))) if e["t"] == "Image"]
    assert [attr_dict(i["c"][0])["width"] for i in images] == ["50%", "50%", "0.5\\columnwidth", ".5\\textwidth"]


def test_table_is_a_table_block_carrying_the_label(tmp_path: Path):
    body = (
        "\\begin{table}\\caption{Cap}\\label{tab:t}\\begin{tabular}{lc}\\toprule A & B \\\\ \\midrule 1 & 2 \\\\ "
        "\\bottomrule\\end{tabular}\\end{table}"
    )
    (table,) = blocks_of(pandoc_json(tmp_path, document(body)), "Table")
    assert table["c"][0][0] == "tab:t"
    assert text_of(table["c"][1]) == "Cap"


def test_equation_environment_and_label_stay_inside_the_math_string(tmp_path: Path):
    body = "\\begin{equation}\nx = 1 \\label{eq:x}\n\\end{equation}"
    (math,) = [e for e in walk(pandoc_json(tmp_path, document(body))) if e["t"] == "Math"]
    assert math["c"][0]["t"] == "DisplayMath"
    assert math["c"][1] == "\\begin{equation}\nx = 1 \\label{eq:x}\n\\end{equation}"


def test_aligned_markers_reach_the_math_string_untouched(tmp_path: Path):
    body = "\\begin{equation}\\begin{aligned}a &= b \\\\ &\\quad + c \\& d\\end{aligned}\\end{equation}"
    (math,) = [e for e in walk(pandoc_json(tmp_path, document(body))) if e["t"] == "Math"]
    assert "a &= b" in math["c"][1] and "\\&" in math["c"][1]


def test_reference_commands_become_links_with_reference_attributes(tmp_path: Path):
    body = (
        "\\section{S}\\label{x}\n\\ref{x} \\eqref{x} \\autoref{x} \\cref{x} \\Cref{x}"
    )
    links = [e for e in walk(pandoc_json(tmp_path, document(body))) if e["t"] == "Link"]
    kinds = [attr_dict(link["c"][0])["reference-type"] for link in links]
    assert kinds == ["ref", "eqref", "ref+label", "ref+label", "ref+Label"]
    assert all(attr_dict(link["c"][0])["reference"] == "x" and link["c"][2][0] == "#x" for link in links)


@pytest.mark.parametrize(
    ("command", "mode"),
    [("cite", "NormalCitation"), ("citep", "NormalCitation"), ("citet", "AuthorInText"),
     ("autocite", "NormalCitation"), ("textcite", "AuthorInText"), ("parencite", "NormalCitation")],
)
def test_citation_commands_become_cite_nodes_with_a_raw_fallback(tmp_path: Path, command: str, mode: str):
    (cite,) = [e for e in walk(pandoc_json(tmp_path, document(f"A \\{command}{{k1}} B"))) if e["t"] == "Cite"]
    assert cite["c"][0][0]["citationId"] == "k1" and cite["c"][0][0]["citationMode"]["t"] == mode
    assert cite["c"][1] == [{"t": "RawInline", "c": ["latex", f"\\{command}{{k1}}"]}]


def test_unknown_commands_vanish_with_their_arguments(tmp_path: Path):
    ast = pandoc_json(tmp_path, document("Hello \\mymacro{LOST} world \\nameref{x} end \\pageref{x}."))
    assert text_of(ast) == "Hello world end ."
    assert not any(e["t"] in ("RawInline", "RawBlock") for e in walk(ast))


def test_raw_tex_extension_keeps_unknown_commands_but_stops_input(tmp_path: Path):
    (tmp_path / "part.tex").write_text("From the part.\n", encoding="utf-8")
    latex = document("Hello \\mymacro{kept}.\n\\input{part}")
    plain = pandoc_json(tmp_path, latex)
    assert "From the part." in text_of(plain)
    raw = pandoc_json(tmp_path, latex, "--from=latex+raw_tex")  # later --from wins
    names = [e["c"][1] for e in walk(raw) if e["t"] in ("RawInline", "RawBlock")]
    assert "\\mymacro{kept}" in names and any(n.startswith("\\input") for n in names)
    assert "From the part." not in text_of(raw)


def test_thebibliography_collapses_into_one_paragraph_and_loses_the_keys(tmp_path: Path):
    body = (
        "\\begin{thebibliography}{99}\\bibitem{a} First. \\bibitem{b} Second.\\end{thebibliography}"
    )
    ast = pandoc_json(tmp_path, document(body))
    (div,) = blocks_of(ast, "Div")
    assert div["c"][0][1] == ["thebibliography"]
    assert len(div["c"][1]) == 1 and div["c"][1][0]["t"] == "Para"
    assert "a" not in [e["c"] for e in walk(div) if e["t"] == "Str"]
    assert [e["c"] for e in walk(div) if e["t"] == "Str"][0] == "99"


def test_hypertarget_wraps_block_content_in_an_identified_div(tmp_path: Path):
    body = "\\begin{l2wbibitem}\\hypertarget{l2w-bib-k}{Text.}\\end{l2wbibitem}"
    (outer,) = blocks_of(pandoc_json(tmp_path, document(body)), "Div")
    inner = outer["c"][1][0]
    assert outer["c"][0][1] == ["l2wbibitem"] and inner["t"] == "Div" and inner["c"][0][0] == "l2w-bib-k"


def test_display_math_inside_a_paragraph_is_an_inline_of_that_paragraph(tmp_path: Path):
    ast = pandoc_json(tmp_path, document("Before\n\\begin{equation}y=2\\end{equation}\nafter"))
    (para,) = blocks_of(ast, "Para")
    assert [i["t"] for i in para["c"] if i["t"] == "Math"] == ["Math"]
    assert text_of(para["c"]).startswith("Before") and text_of(para["c"]).endswith("after")


def test_local_style_macros_are_expanded_by_pandoc(tmp_path: Path):
    (tmp_path / "localstyle.sty").write_text("\\newcommand{\\term}[1]{\\textbf{TERM #1}}\n", encoding="utf-8")
    latex = "\\documentclass{article}\n\\usepackage{localstyle}\n\\begin{document}\n\\term{x}\n\\end{document}\n"
    assert text_of(pandoc_json(tmp_path, latex)) == "TERM x"
