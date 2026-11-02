from __future__ import annotations

from pathlib import Path

from projectkit import make_project

from latex2word.diagnostics import Severity
from latex2word.labels import check_labels, scan_labels
from latex2word.source_graph import build_source_graph


def scan(tmp_path: Path, files: dict[str, str]):
    root = make_project(tmp_path, files)
    return scan_labels(build_source_graph(root, root / "main.tex"))


def test_labels_and_references_are_found_across_files_with_locations(tmp_path: Path):
    result = scan(
        tmp_path,
        {
            "main.tex": "\\begin{document}\n\\section{A}\\label{sec:a}\n\\input{part}\n\\ref{fig:x} \\eqref{eq:y} \\cref{sec:a,fig:x}\n\\end{document}",
            "part.tex": "\\begin{figure}\\caption{c}\\label{fig:x}\\end{figure}\n% \\label{commented}\n\\autoref{sec:a}",
        },
    )
    assert [(d.label, str(d.location)) for d in result.definitions] == [("sec:a", "main.tex:2"), ("fig:x", "part.tex:1")]
    assert [(r.label, r.command) for r in result.references] == [
        ("fig:x", "ref"), ("eq:y", "eqref"), ("sec:a", "cref"), ("fig:x", "cref"), ("sec:a", "autoref"),
    ]


def test_duplicates_dangling_references_and_unreferenced_targets(tmp_path: Path):
    result = scan(
        tmp_path,
        {
            "main.tex": "\\begin{document}\\label{a}\\label{a}\\label{lonely}\n\\ref{a} \\ref{ghost}\n\\eqref{ghost}\n\\end{document}",
        },
    )
    findings = check_labels(result)
    by_code = {f.code: f for f in findings}
    assert by_code["L2W-E-LABEL-001"].message.startswith('duplicate label "a"')
    assert [f.location.line for f in findings if f.code == "L2W-E-REF-001"] == [2, 3]
    assert by_code["L2W-I-LABEL-003"].severity is Severity.INFO and "lonely" in by_code["L2W-I-LABEL-003"].message
    assert [f.code for f in findings if f.severity is Severity.ERROR] == ["L2W-E-LABEL-001", "L2W-E-REF-001", "L2W-E-REF-001"]


def test_a_consistent_document_has_no_errors(tmp_path: Path):
    findings = check_labels(scan(tmp_path, {"main.tex": "\\begin{document}\\label{a}\\ref{a}\\end{document}"}))
    assert findings == []
