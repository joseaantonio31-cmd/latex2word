from __future__ import annotations

from pathlib import Path

import pytest
from projectkit import make_project

from latex2word.bibliography import parse_bib_keys, plan_bibliography, scan_citations
from latex2word.config import BibliographySection
from latex2word.diagnostics import Severity
from latex2word.source_graph import build_source_graph

GENERIC_BIB = {"mode": "auto", "heading": "References", "manual": {}}
BIB = "@article{a, title={A}}\n@book{b,\n title={B}}\n@string{x = {y}}\n@comment{c, ignored}\n@misc{c, title={C}}\n"
INLINE = (
    "\\begin{thebibliography}{9}\n\\bibitem{a} First.\n\\bibitem[Lbl]{b} Second \\& more.\n\\end{thebibliography}\n"
)


def plan(tmp_path: Path, body: str, files: dict | None = None, config: BibliographySection | None = None, **kwargs):
    root = make_project(tmp_path, {"main.tex": f"\\documentclass{{article}}\\begin{{document}}\n{body}\n\\end{{document}}\n", **(files or {})})
    graph = build_source_graph(root, root / "main.tex")
    profile = {**GENERIC_BIB, **kwargs}
    return plan_bibliography(graph, config or BibliographySection(), profile, None, root), graph


def by_code(result) -> dict[str, list]:
    out: dict[str, list] = {}
    for finding in result.findings:
        out.setdefault(finding.code, []).append(finding)
    return out


def test_bib_keys_skip_non_entries_and_report_duplicates():
    keys, duplicates = parse_bib_keys(BIB + "@article{a, title={again}}\n")
    assert keys == ["a", "b", "c"] and duplicates == ["a"]


def test_citation_scan_covers_the_common_forms_with_locations(tmp_path: Path):
    root = make_project(
        tmp_path,
        {"main.tex": "\\begin{document}\n\\cite{a} \\citep[p.~2]{b,c}\n\\citet{d}\\autocite{e}\\nocite{*}\\textcite[see][]{f}\\cites{g}{h}\n\\end{document}\n"},
    )
    uses = scan_citations(build_source_graph(root, root / "main.tex"))
    assert [(u.key, u.command) for u in uses] == [
        ("a", "cite"), ("b", "citep"), ("c", "citep"), ("d", "citet"), ("e", "autocite"), ("*", "nocite"),
        ("f", "textcite"), ("g", "cites"), ("h", "cites"),
    ]
    assert uses[0].location.line == 2 and uses[3].location.line == 3


def test_auto_mode_with_an_external_bib_selects_citeproc_and_verifies_keys(tmp_path: Path):
    result, _ = plan(tmp_path, "\\cite{a,b}\\bibliography{refs}", {"refs.bib": BIB})
    assert result.mode == "citeproc" and result.files == ["refs.bib"] and result.expected_entries == 2
    assert result.defined == {"a", "b", "c"} and "L2W-I-BIB-008" in by_code(result)
    assert [e.kind for e in result.edits] == ["bibliography-remove"]  # citeproc gets the files explicitly
    assert not [f for f in result.findings if f.severity is Severity.ERROR]


def test_missing_citation_key_is_an_error_with_its_location(tmp_path: Path):
    result, _ = plan(tmp_path, "\\cite{a}\n\\citep{ghost}\\bibliography{refs}", {"refs.bib": BIB})
    (finding,) = by_code(result)["L2W-E-CITE-001"]
    assert "'ghost'" in finding.message and finding.location.file == "main.tex" and finding.location.line == 3
    assert result.expected_entries == 1


def test_nocite_star_expects_every_entry(tmp_path: Path):
    result, _ = plan(tmp_path, "\\cite{a}\\nocite{*}\\bibliography{refs}", {"refs.bib": BIB})
    assert result.expected_entries == 3 and "L2W-E-CITE-001" not in by_code(result)


def test_auto_mode_with_an_inline_bibliography_selects_manual(tmp_path: Path):
    result, _ = plan(tmp_path, "\\cite{a,b}\n" + INLINE)
    assert result.mode == "manual" and result.inline_keys == ["a", "b"] and result.expected_entries == 2
    (edit,) = [e for e in result.edits if e.kind == "bibliography-inline"]
    assert "\\hypertarget{l2w-bib-a}{First.}" in edit.replacement and "Second \\& more." in edit.replacement
    assert "thebibliography" not in edit.replacement
    assert "L2W-W-BIB-006" in by_code(result)  # custom \bibitem label is not supported


def test_both_sources_without_explicit_precedence_is_an_ambiguity_error(tmp_path: Path):
    result, _ = plan(tmp_path, "\\cite{a}\\bibliography{refs}\n" + INLINE, {"refs.bib": BIB})
    assert result.mode == "none" and [f.code for f in result.findings if f.severity is Severity.ERROR] == ["L2W-E-BIB-001"]


@pytest.mark.parametrize(("prefer", "mode"), [("external", "citeproc"), ("inline", "manual")])
def test_explicit_precedence_resolves_the_ambiguity(tmp_path: Path, prefer: str, mode: str):
    result, _ = plan(tmp_path, "\\cite{a}\\bibliography{refs}\n" + INLINE, {"refs.bib": BIB}, BibliographySection(prefer=prefer))
    assert result.mode == mode and not [f for f in result.findings if f.severity is Severity.ERROR]
    assert any(e.kind == "bibliography-remove" for e in result.edits)  # the losing source is removed from the staged copy


def test_explicit_mode_overrides_what_the_source_contains(tmp_path: Path):
    config = BibliographySection(mode="manual")
    result, _ = plan(tmp_path, "\\cite{a}\\bibliography{refs}\n" + INLINE, {"refs.bib": BIB}, config)
    assert result.mode == "manual" and not [f for f in result.findings if f.severity is Severity.ERROR]
    result, _ = plan(tmp_path, "\\cite{a}\n" + INLINE, None, BibliographySection(mode="citeproc"))
    assert result.mode == "none" and "L2W-E-BIB-002" in by_code(result)


def test_citations_without_any_bibliography_are_an_error_but_no_citations_is_fine(tmp_path: Path):
    result, _ = plan(tmp_path, "\\cite{a}")
    assert result.mode == "none" and "L2W-E-BIB-002" in by_code(result)
    result, _ = plan(tmp_path, "No citations here.")
    assert result.mode == "none" and result.findings == []


def test_configured_files_replace_the_ones_named_by_the_source_and_support_several(tmp_path: Path):
    files = {"one.bib": "@article{a,title={A}}", "two.bib": "@article{b,title={B}}", "refs.bib": "@article{z,title={Z}}"}
    config = BibliographySection(files=("one.bib", "two.bib"))
    result, _ = plan(tmp_path, "\\cite{a,b}\\bibliography{refs}", files, config)
    assert result.mode == "citeproc" and result.files == ["one.bib", "two.bib"] and result.defined == {"a", "b"}
    assert result.expected_entries == 2 and not [f for f in result.findings if f.severity is Severity.ERROR]


def test_bibliography_file_names_are_not_assumed(tmp_path: Path):
    result, _ = plan(tmp_path, "\\cite{x}\\bibliography{my-library}", {"my-library.bib": "@article{x,title={X}}"})
    assert result.files == ["my-library.bib"] and result.mode == "citeproc"


def test_addbibresource_is_a_bibliography_source(tmp_path: Path):
    result, _ = plan(tmp_path, "\\cite{x}\\addbibresource{lib.bib}", {"lib.bib": "@article{x,title={X}}"})
    assert result.mode == "citeproc" and result.files == ["lib.bib"]
    assert [e.kind for e in result.edits] == ["bibliography-remove"]


def test_duplicate_bibitem_keys_and_missing_csl_are_reported(tmp_path: Path):
    dup = "\\begin{thebibliography}{9}\\bibitem{a} One.\\bibitem{a} Two.\\end{thebibliography}"
    result, _ = plan(tmp_path, "\\cite{a}" + dup)
    assert "L2W-E-BIB-003" in by_code(result)
    root = make_project(tmp_path / "x", {"main.tex": "\\begin{document}\\cite{x}\\bibliography{r}\\end{document}", "r.bib": "@article{x,title={X}}"})
    graph = build_source_graph(root, root / "main.tex")
    missing_csl = plan_bibliography(graph, BibliographySection(csl=tmp_path / "nope.csl"), GENERIC_BIB, None, root)
    assert "L2W-E-BIB-013" in by_code(missing_csl)
    no_csl = plan_bibliography(graph, BibliographySection(), GENERIC_BIB, None, root)
    assert by_code(no_csl)["L2W-I-BIB-007"][0].severity is Severity.INFO
