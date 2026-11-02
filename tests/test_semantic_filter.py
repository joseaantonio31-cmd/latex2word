"""Numbering, cross-references and labels: the latex2word Lua filter run by the real Pandoc."""

from __future__ import annotations

from pathlib import Path

import pytest
from pandockit import blocks_of, codes, document, requires_pandoc, run_filter, text_of, walk

pytestmark = requires_pandoc

FIG = "\\begin{{figure}}\\includegraphics{{x.png}}\\caption{{{cap}}}\\label{{{label}}}\\end{{figure}}\n"
TAB = "\\begin{{table}}\\caption{{{cap}}}\\label{{{label}}}\\begin{{tabular}}{{l}}A\\end{{tabular}}\\end{{table}}\n"
EQ = "\n\\begin{{equation}}\n{body} \\label{{{label}}}\n\\end{{equation}}\n\n"


def header_texts(ast) -> list[str]:
    return [text_of(h["c"][2]) for h in blocks_of(ast, "Header")]


def captions(ast, tag: str) -> list[str]:
    return [text_of(b["c"][1]) for b in blocks_of(ast, tag)]


def ids(ast) -> set[str]:
    found = set()
    for element in walk(ast):
        if element["t"] in ("Header", "Span", "Div", "Figure", "Table"):
            attr = element["c"][0] if element["t"] != "Header" else element["c"][1]
            if attr[0]:
                found.add(attr[0])
    return found


def link_targets(ast) -> list[str]:
    return [e["c"][2][0][1:] for e in walk(ast) if e["t"] == "Link" and e["c"][2][0].startswith("#")]


# ------------------------------------------------------------------------------ sections


def test_sections_are_numbered_hierarchically_and_starred_ones_are_not(tmp_path: Path):
    body = (
        "\\section{A}\\subsection{B}\\subsection{C}\\section*{Data Availability}\\section{D}"
        "\\subsection{E}\\section*{Supplementary Material}\\section{F}"
    )
    ast, report = run_filter(tmp_path, document(body))
    assert header_texts(ast) == [
        "1 A", "1.1 B", "1.2 C", "Data Availability", "2 D", "2.1 E", "Supplementary Material", "3 F",
    ]
    assert [s["text"] for s in report["sequences"]["sections"]] == ["1", "1.1", "1.2", "2", "2.1", "3"]
    assert [h["title"] for h in report["unnumbered_headings"]] == ["Data Availability", "Supplementary Material"]


def test_ieee_profile_numbers_sections_in_the_heading_itself_without_post_processing(tmp_path: Path):
    ast, report = run_filter(
        tmp_path, document("\\section{One}\\subsection{Two}\\subsection{Three}\\section{Four}\\subsection{Five}"),
        profile="ieee-conference",
    )
    assert header_texts(ast) == ["I. One", "A. Two", "B. Three", "II. Four", "A. Five"]
    assert [s["n"] for s in report["sequences"]["sections"]] == [1, 1, 2, 2, 1]


def test_unnumbered_heading_does_not_consume_a_counter_in_any_profile(tmp_path: Path):
    body = "\\section{A}\\section*{Zeta}\\section{B}"
    for profile in ("generic", "ieee-conference"):
        ast, _ = run_filter(tmp_path, document(body), profile=profile)
        assert [t.split()[-1] for t in header_texts(ast)] == ["A", "Zeta", "B"]
        assert header_texts(ast)[1] == "Zeta"
        assert header_texts(ast)[2].startswith({"generic": "2", "ieee-conference": "II."}[profile])


# ---------------------------------------------------------------------------- counters


def test_figure_table_and_equation_counters_are_continuous_across_sections(tmp_path: Path):
    body = ""
    for index in range(3):
        body += f"\\section{{S{index}}}\n"
        body += FIG.format(cap=f"cap{index}", label=f"fig:{index}") + TAB.format(cap=f"tab{index}", label=f"tab:{index}")
        body += EQ.format(body=f"x_{index}=1", label=f"eq:{index}")
    ast, report = run_filter(tmp_path, document(body), profile="ieee-conference")
    sequences = report["sequences"]
    assert [s["n"] for s in sequences["figures"]] == [1, 2, 3]
    assert [s["text"] for s in sequences["figures"]] == ["Fig. 1.", "Fig. 2.", "Fig. 3."]
    assert [s["text"] for s in sequences["tables"]] == ["TABLE I", "TABLE II", "TABLE III"]
    assert [s["text"] for s in sequences["equations"]] == ["(1)", "(2)", "(3)"]
    assert captions(ast, "Figure") == ["Fig. 1. cap0", "Fig. 2. cap1", "Fig. 3. cap2"]


def test_reset_policy_restarts_a_counter_at_each_numbered_section(tmp_path: Path):
    body = (
        "\\section{A}" + FIG.format(cap="a", label="f1") + FIG.format(cap="b", label="f2")
        + "\\section*{Not a reset}" + FIG.format(cap="c", label="f3")
        + "\\section{B}" + FIG.format(cap="d", label="f4")
    )
    _, report = run_filter(
        tmp_path, document(body),
        numbering_overrides={"figures.reset": "section", "figures.format": "Figure {n1}.{n}"},
    )
    assert [s["text"] for s in report["sequences"]["figures"]] == ["Figure 1.1", "Figure 1.2", "Figure 1.3", "Figure 2.1"]


def test_numeral_styles_roman_latin_and_forced_fields(tmp_path: Path):
    body = "".join(TAB.format(cap=f"t{i}", label=f"t{i}") for i in range(5))
    _, report = run_filter(
        tmp_path, document(body),
        numbering_overrides={"tables.numeral": "alpha", "tables.format": "Tab {n}/{Roman}/{roman}/{Alpha}/{arabic}"},
    )
    assert [s["text"] for s in report["sequences"]["tables"]][3] == "Tab d/IV/iv/D/4"


def test_counter_beyond_the_numeral_range_is_an_explicit_error(tmp_path: Path):
    body = "".join(FIG.format(cap=f"f{i}", label=f"f{i}") for i in range(27))
    _, report = run_filter(tmp_path, document(body), numbering_overrides={"figures.numeral": "Alpha"})
    assert "L2W-E-NUM-001" in codes(report)


def test_figures_and_tables_without_captions_are_not_numbered(tmp_path: Path):
    body = "\\begin{figure}\\includegraphics{x.png}\\label{fig:bare}\\end{figure}" + FIG.format(cap="c", label="fig:c")
    ast, report = run_filter(tmp_path, document(body))
    assert [s["text"] for s in report["sequences"]["figures"]] == ["Figure 1"]
    label = next(entry for entry in report["labels"] if entry["label"] == "fig:bare")
    assert label["numbered"] is False


def test_caption_label_is_a_bookmark_span_and_the_figure_keeps_one_identifier(tmp_path: Path):
    ast, _ = run_filter(tmp_path, document(FIG.format(cap="Cap", label="fig:a")))
    (figure,) = blocks_of(ast, "Figure")
    assert figure["c"][0][0] == ""  # the label moved to the caption span: one bookmark, not two
    spans = [e for e in walk(figure["c"][1]) if e["t"] == "Span"]
    assert [s["c"][0][0] for s in spans] == ["fig:a"] and text_of(spans[0]) == "Figure 1"


# ------------------------------------------------------------------------- references


def test_reference_commands_use_the_profile_formats(tmp_path: Path):
    body = (
        "\\section{S}\\label{sec:s}\n" + FIG.format(cap="c", label="fig:a") + TAB.format(cap="t", label="tab:a")
        + EQ.format(body="y=1", label="eq:a")
        + "Refs: \\ref{sec:s}|\\ref{fig:a}|\\ref{tab:a}|\\ref{eq:a}|\\eqref{eq:a}|\\autoref{fig:a}|\\cref{tab:a}|\\autoref{sec:s}"
    )
    generic, report = run_filter(tmp_path, document(body))
    assert [r["text"] for r in report["refs"]] == ["1", "1", "1", "1", "(1)", "Figure 1", "Table 1", "Section 1"]
    ieee, report = run_filter(tmp_path, document(body), profile="ieee-conference")
    assert [r["text"] for r in report["refs"]] == ["I", "1", "I", "1", "(1)", "Fig. 1", "Table I", "Section I"]
    paragraph = text_of(blocks_of(ieee, "Para")[-1])
    assert paragraph.endswith("Refs: I|1|I|1|(1)|Fig. 1|Table I|Section I")


def test_every_resolved_link_targets_an_existing_identifier(tmp_path: Path):
    body = (
        "\\section{S}\\label{sec:s}\\subsection{T}\\label{sec:t}\n" + FIG.format(cap="c", label="fig:a")
        + TAB.format(cap="t", label="tab:a") + EQ.format(body="y=1", label="eq:a")
        + "\\ref{sec:s} \\ref{sec:t} \\ref{fig:a} \\ref{tab:a} \\eqref{eq:a}"
    )
    ast, report = run_filter(tmp_path, document(body))
    assert all(r["resolved"] for r in report["refs"])
    assert set(link_targets(ast)) <= ids(ast) and len(link_targets(ast)) == 5


def test_equation_references_are_parenthesised_and_follow_the_equation_counter(tmp_path: Path):
    body = EQ.format(body="a=1", label="eq:a") + EQ.format(body="b=2", label="eq:b") + "\\eqref{eq:a} and \\eqref{eq:b}"
    ast, report = run_filter(tmp_path, document(body))
    assert text_of(blocks_of(ast, "Para")[-1]) == "(1) and (2)"
    assert [e["number"] for e in report["labels"] if e["kind"] == "equation"] == [1, 2]


def test_undefined_label_is_an_error_and_never_a_silent_number(tmp_path: Path):
    ast, report = run_filter(tmp_path, document("See \\ref{nowhere}."))
    assert codes(report) == ["L2W-E-REF-001"] and "??" in text_of(ast)


def test_duplicate_labels_are_reported(tmp_path: Path):
    body = FIG.format(cap="a", label="dup") + TAB.format(cap="b", label="dup")
    _, report = run_filter(tmp_path, document(body))
    assert "L2W-E-LABEL-001" in codes(report)


def test_reference_to_an_unnumbered_target_is_an_error(tmp_path: Path):
    _, report = run_filter(tmp_path, document("\\section*{Notes}\\label{sec:notes}\nSee \\ref{sec:notes}."))
    assert "L2W-E-REF-004" in codes(report)


def test_eqref_to_a_non_equation_warns_about_the_mismatch(tmp_path: Path):
    ast, report = run_filter(tmp_path, document(FIG.format(cap="c", label="fig:a") + "\\eqref{fig:a}"))
    assert "L2W-W-REF-003" in codes(report) and text_of(blocks_of(ast, "Para")[-1]) == "(1)"


def test_targets_without_references_are_not_errors(tmp_path: Path):
    _, report = run_filter(tmp_path, document(FIG.format(cap="c", label="fig:lonely")))
    entry = next(e for e in report["labels"] if e["label"] == "fig:lonely")
    assert entry["refs"] == 0 and report["diagnostics"] == []


def test_filter_ignores_heading_identifiers_that_are_not_source_labels(tmp_path: Path):
    _, report = run_filter(tmp_path, document("\\section{Auto id}\\section{Other}\\label{sec:o}"), source_labels=["sec:o"])
    assert [e["label"] for e in report["labels"]] == ["sec:o"]


# --------------------------------------------------------------------------- equations


def test_numbered_equations_are_paragraphs_with_number_and_bookmark_and_no_label_text(tmp_path: Path):
    body = "Before" + EQ.format(body="x=1", label="eq:x") + "After text."
    ast, report = run_filter(tmp_path, document(body))
    divs = [b for b in ast["blocks"] if b["t"] == "Div"]
    assert [d["c"][0][2][0][1] for d in divs] == ["Equation", "Equation Number"]
    assert text_of(divs[1]) == "(1)"
    (math,) = [e for e in walk(divs[0]) if e["t"] == "Math"]
    assert "\\label" not in math["c"][1] and "equation" not in math["c"][1]
    assert "eq:x" in ids(ast)
    assert [text_of(b) for b in blocks_of(ast, "Para")] == ["Before", "After text."]


def test_unnumbered_displays_are_untouched_and_consume_no_number(tmp_path: Path):
    body = (
        "\n\\begin{equation*}\nu=1\n\\end{equation*}\n\n\\[ v=2 \\]\n\n"
        "\\begin{equation}\nw=3 \\nonumber\n\\end{equation}\n\n" + EQ.format(body="z=4", label="eq:z")
    )
    _, report = run_filter(tmp_path, document(body))
    assert [s["text"] for s in report["sequences"]["equations"]] == ["(1)"]


def test_label_on_an_unnumbered_equation_is_an_error(tmp_path: Path):
    body = "\n\\begin{equation*}\nu=1 \\label{eq:u}\n\\end{equation*}\n"
    _, report = run_filter(tmp_path, document(body))
    assert "L2W-E-LABEL-002" in codes(report)


def test_equation_counter_reset_policy_is_configurable(tmp_path: Path):
    body = "\\section{A}" + EQ.format(body="a=1", label="e1") + EQ.format(body="b=2", label="e2") + "\\section{B}" + EQ.format(body="c=3", label="e3")
    _, report = run_filter(
        tmp_path, document(body), numbering_overrides={"equations.reset": "section", "equations.format": "({n1}.{n})"}
    )
    assert [s["text"] for s in report["sequences"]["equations"]] == ["(1.1)", "(1.2)", "(2.1)"]


@pytest.mark.parametrize("profile", ["generic", "ieee-conference"])
def test_equation_numbering_does_not_depend_on_other_counters(tmp_path: Path, profile: str):
    body = FIG.format(cap="c", label="f") + EQ.format(body="a=1", label="e1") + FIG.format(cap="d", label="g") + EQ.format(body="b=2", label="e2")
    _, report = run_filter(tmp_path, document(body), profile=profile)
    assert [s["n"] for s in report["sequences"]["equations"]] == [1, 2]


# ------------------------------------------------------------------ manual bibliography


MANUAL = (
    "See \\cite{b} and \\cite{a,b} and \\cite[p.~3]{a}.\n"
    "\\begin{l2wbibliography}\n"
    "\\begin{l2wbibitem}\\hypertarget{l2w-bib-a}{First entry.}\\end{l2wbibitem}\n"
    "\\begin{l2wbibitem}\\hypertarget{l2w-bib-b}{Second entry.}\\end{l2wbibitem}\n"
    "\\end{l2wbibliography}"
)


def test_manual_bibliography_is_numbered_by_entry_order_and_cites_link_to_entries(tmp_path: Path):
    ast, report = run_filter(tmp_path, document(MANUAL), mode="manual")
    paragraph = text_of(blocks_of(ast, "Para")[0])
    assert paragraph == "See [2] and [1, 2] and [1, p.\u00a03]."  # \cite[p.~3]{a}: the tie is a no-break space
    assert header_texts(ast) == ["References"]
    (refs,) = [b for b in blocks_of(ast, "Div") if b["c"][0][0] == "refs"]
    entries = refs["c"][1]
    assert [text_of(e) for e in entries] == ["[1] First entry.", "[2] Second entry."]
    assert [e["c"][0][0] for e in entries] == ["ref-a", "ref-b"]
    assert set(link_targets(ast)) == {"ref-a", "ref-b"} and set(link_targets(ast)) <= ids(ast)
    assert report["bibliography"]["manual_entries"] == ["a", "b"]


def test_manual_citation_of_an_unknown_key_is_an_error(tmp_path: Path):
    body = MANUAL.replace("\\cite{b}", "\\cite{missing}")
    _, report = run_filter(tmp_path, document(body), mode="manual")
    assert "L2W-E-CITE-001" in codes(report)


def test_citeproc_mode_leaves_citations_for_citeproc_and_records_the_keys(tmp_path: Path):
    ast, report = run_filter(tmp_path, document("A \\cite{x} B \\citep{x,y}."), mode="citeproc")
    assert [e["t"] for e in walk(ast) if e["t"] == "Cite"] == ["Cite", "Cite"]
    assert report["citations"] == [{"id": "x", "count": 2}, {"id": "y", "count": 1}]


def test_inline_bibliography_in_citeproc_mode_is_reported(tmp_path: Path):
    _, report = run_filter(tmp_path, document(MANUAL), mode="citeproc")
    assert "L2W-E-BIB-005" in codes(report)
