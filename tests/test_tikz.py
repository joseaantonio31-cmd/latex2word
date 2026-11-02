from __future__ import annotations

import pytest

from latex2word.tikz import (
    TikzExtractionError,
    build_render_document,
    derive_render_preamble,
    extract_environment,
    extract_environments,
)


def test_nested_braces_do_not_end_the_environment_early():
    body = "\\begin{tikzpicture}\\node {a {b} \\} c};\\end{tikzpicture}"
    text = f"before\n{body}\nafter"
    assert extract_environment(text, "tikzpicture").source == body


def test_same_name_nesting_returns_the_outer_environment_only():
    inner = "\\begin{tikzpicture}\\end{tikzpicture}"
    outer = f"\\begin{{tikzpicture}}\\node{{{inner}}};\\end{{tikzpicture}}"
    found = extract_environments(f"x {outer} y", ["tikzpicture"])
    assert [e.source for e in found] == [outer]


def test_other_environment_nested_inside_is_kept_verbatim():
    drawing = "\\begin{circuitikz}\\begin{scope}\\draw (0,0)--(1,0);\\end{scope}\\end{circuitikz}"
    assert extract_environment(drawing, "circuitikz").source == drawing


def test_multiple_environments_in_document_order_with_lines():
    text = "\\begin{tikzpicture}1\\end{tikzpicture}\n\n\\begin{circuitikz}2\\end{circuitikz}\n\\begin{tikzpicture}3\\end{tikzpicture}"
    found = extract_environments(text)
    assert [(e.name, e.line) for e in found] == [("tikzpicture", 1), ("circuitikz", 3), ("tikzpicture", 4)]
    assert extract_environment(text, "tikzpicture", occurrence=1).source.endswith("3\\end{tikzpicture}")


def test_commented_end_does_not_close_the_environment():
    text = "\\begin{tikzpicture}\n% \\end{tikzpicture}\n\\draw (0,0);\n\\end{tikzpicture}"
    assert extract_environment(text, "tikzpicture").source == text


@pytest.mark.parametrize(
    "text",
    [
        "\\begin{tikzpicture}\\draw (0,0);",
        "\\end{tikzpicture}",
        "\\begin{tikzpicture}\\end{circuitikz}",
        "\\begin{tikzpicture}\\begin{tikzpicture}\\end{tikzpicture}",
    ],
)
def test_malformed_environments_raise_with_a_line(text: str):
    with pytest.raises(TikzExtractionError) as info:
        extract_environments(text)
    assert info.value.line == 1


def test_missing_occurrence_raises():
    with pytest.raises(TikzExtractionError, match="not found"):
        extract_environment("\\begin{tikzpicture}\\end{tikzpicture}", "circuitikz")


MAIN = r"""
\documentclass{IEEEtran}
\usepackage[T1]{fontenc}
\usepackage{cite}
\usepackage[american]{circuitikz}
\usepackage{tikz}
% \usepackage{pgfplots}
\usetikzlibrary{arrows.meta,fit}
\tikzset{mystyle/.style={thick}}
\definecolor{brand}{RGB}{1,2,3}
\newcommand{\Vamp}{V_{\mathrm{AMP}}}
\newcommand{\unused}{x}
\renewcommand{\topfraction}{0.92}
\begin{document}
\usepackage{amsmath}
\end{document}
"""


def test_preamble_is_derived_not_hardcoded():
    drawing = "\\begin{circuitikz}\\node {$\\Vamp$};\\end{circuitikz}"
    preamble = derive_render_preamble(MAIN, drawing)
    assert preamble.lines == (
        "\\usepackage[american]{circuitikz}",
        "\\usepackage{tikz}",
        "\\usetikzlibrary{arrows.meta,fit}",
        "\\tikzset{mystyle/.style={thick}}",
        "\\definecolor{brand}{RGB}{1,2,3}",
        "\\newcommand{\\Vamp}{V_{\\mathrm{AMP}}}",
    )
    assert "fontenc" not in "".join(preamble.lines)  # T1 would select EC bitmap fonts
    assert preamble.packages == {"circuitikz", "tikz"}


def test_render_document_wraps_with_dvisvgm_driver_lmodern_and_extras():
    preamble = derive_render_preamble(MAIN, "")
    document = build_render_document("\\begin{circuitikz}X\\end{circuitikz}", preamble, "circuitikz", ["\\usepackage{foo}"])
    lines = document.splitlines()
    assert lines[:3] == [
        "\\documentclass[border=2pt]{standalone}",
        "\\def\\pgfsysdriver{pgfsys-dvisvgm.def}",
        "\\usepackage{lmodern}",
    ]
    assert lines.index("\\usepackage{foo}") < lines.index("\\begin{document}")
    assert lines[lines.index("\\begin{document}") + 1] == "\\begin{circuitikz}X\\end{circuitikz}"
    assert document.endswith("\\end{document}\n")


def test_missing_tikz_package_is_added_for_the_drawing_environment():
    preamble = derive_render_preamble("\\documentclass{article}\\begin{document}\\end{document}", "")
    assert "\\usepackage{tikz}" in build_render_document("x", preamble, "tikzpicture")
    assert "\\usepackage{circuitikz}" in build_render_document("x", preamble, "circuitikz")
