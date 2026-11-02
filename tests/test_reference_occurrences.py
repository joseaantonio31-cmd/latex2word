"""F-04 (phase 2): reference loss is detected per occurrence, not per label.

Every reference command of the source gets an occurrence identity in the staged copy (``\\ref{l2wocc<n>:a}``); the
filter reports the identity of each reference that reached the AST; the build reports every identity that did not.
The cases A-E are the acceptance cases of reports/audit/remediation-phase2.md. "Dropping" an occurrence is simulated by
leaving its id out of the filter report (the reader-level loss itself was reproduced with Pandoc 3.1.3, which drops
``\\Cref``); the filter's side of the contract is tested with the real Pandoc and filter below.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pandockit import document, requires_pandoc, run_filter
from projectkit import make_project

from latex2word.build import _lost_references, _reference_markers
from latex2word.labels import plan_reference_markers
from latex2word.rewrite import Edit
from latex2word.source_graph import SourceLocation, build_source_graph


def graph_of(tmp_path: Path, body: str, preamble: str = ""):
    tex = f"\\documentclass{{article}}\n{preamble}\n\\begin{{document}}\n\\section{{A}}\\label{{a}}\n{body}\n\\end{{document}}\n"
    root = make_project(tmp_path, {"main.tex": tex})
    return build_source_graph(root, root / "main.tex")


def surviving(occurrences, *ids):
    return {"refs": [{"label": o.label, "occurrence": o.id} for o in occurrences if o.id in ids]}


def test_a_dropped_cref_is_reported_although_a_ref_to_the_same_label_survives(tmp_path: Path):
    _, occ = plan_reference_markers(graph_of(tmp_path, "As \\Cref{a} shows,\nsee also \\ref{a}."))
    assert [(o.command, o.label, o.location.line) for o in occ] == [("Cref", "a", 5), ("ref", "a", 6)]
    [finding] = _lost_references(occ, surviving(occ, 2))
    assert finding.code == "L2W-E-REF-012" and "\\Cref{a}" in finding.message
    assert finding.location == SourceLocation("main.tex", 5)


def test_b_one_of_two_identical_references_dropped_is_reported(tmp_path: Path):
    _, occ = plan_reference_markers(graph_of(tmp_path, "\\ref{a} and \\ref{a}."))
    assert len(occ) == 2
    assert len(_lost_references(occ, surviving(occ, 1))) == 1
    assert len(_lost_references(occ, surviving(occ, 2))) == 1


def test_c_reference_in_an_unused_macro_definition_does_not_count(tmp_path: Path):
    edits, occ = plan_reference_markers(
        graph_of(tmp_path, "Text.", preamble="\\newcommand\\seeone{see \\ref{a}}\\def\\g#1{\\ref{#1}}")
    )
    assert edits == [] and occ == []
    assert _lost_references(occ, {"refs": []}) == []


def test_d_three_surviving_references_to_one_label_pass(tmp_path: Path):
    _, occ = plan_reference_markers(graph_of(tmp_path, "\\ref{a}, \\ref{a} and \\ref{a}."))
    assert len(occ) == 3 and _lost_references(occ, surviving(occ, 1, 2, 3)) == []


def test_e_different_commands_to_one_label_all_surviving_pass(tmp_path: Path):
    body = "\\ref{a} \\eqref{a} \\autoref{a} \\cref{a} \\Cref{a} \\vref{a}"
    _, occ = plan_reference_markers(graph_of(tmp_path, body))
    assert [o.command for o in occ] == ["ref", "eqref", "autoref", "cref", "Cref", "vref"]
    assert _lost_references(occ, surviving(occ, *range(1, 7))) == []


def test_comma_lists_give_one_occurrence_per_label_and_one_edit(tmp_path: Path):
    edits, occ = plan_reference_markers(graph_of(tmp_path, "\\cref{a, b}", preamble=""))
    assert [o.label for o in occ] == ["a", "b"]
    assert [e.replacement for e in edits] == ["l2wocc1:a,l2wocc2:b"]


def test_generated_or_unmarked_references_in_the_report_are_ignored(tmp_path: Path):
    _, occ = plan_reference_markers(graph_of(tmp_path, "\\ref{a}"))
    report = {"refs": [{"label": "a", "occurrence": 1}, {"label": "generated"}, {"label": "x", "occurrence": None}]}
    assert _lost_references(occ, report) == []


def test_markers_inside_or_overlapping_a_content_rewrite_are_not_checked(tmp_path: Path):
    graph = graph_of(tmp_path, "\\ref{a} then \\ref{a}")
    edits, occ = plan_reference_markers(graph)
    first, second = edits
    outer = Edit("main.tex", first.start - 5, first.end + 1, "X", "figure-tikz", "replaced by its SVG")
    partial = Edit("main.tex", second.start, second.end + 3, "Y", "other", "overlaps the marker")
    kept, checked = _reference_markers(graph, [outer, partial])
    assert kept == [] and checked == []


def test_non_literal_labels_are_not_marked(tmp_path: Path):
    _, occ = plan_reference_markers(graph_of(tmp_path, "\\ref{\\mylabel} \\ref{a}"))
    assert [o.label for o in occ] == ["a"]


@requires_pandoc
def test_filter_reports_the_occurrence_of_each_surviving_reference(tmp_path: Path):
    body = "\\section{S}\\label{a}\nSee \\ref{l2wocc7:a} and \\ref{l2wocc8:a}, \\ref{l2wocc9:nowhere}."
    _, report = run_filter(tmp_path, document(body), source_labels=["a"])
    refs = [(r["label"], r.get("occurrence"), r["resolved"]) for r in report["refs"]]
    assert refs == [("a", 7, True), ("a", 8, True), ("nowhere", 9, False)]


@requires_pandoc
def test_marked_labels_resolve_to_the_same_text_and_target_as_unmarked_ones(tmp_path: Path):
    from pandockit import walk

    def links(label_text: str):
        ast, _ = run_filter(tmp_path, document(f"\\section{{S}}\\label{{a}}\nSee \\ref{{{label_text}}}."), source_labels=["a"])
        return [(l["c"][2][0], "".join(i["c"] for i in l["c"][1] if i["t"] == "Str")) for l in walk(ast) if l["t"] == "Link"]

    assert links("l2wocc3:a") == links("a")
