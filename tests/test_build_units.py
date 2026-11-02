"""Pieces of the build orchestrator that need no Pandoc."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from latex2word import build
from latex2word.diagnostics import Severity
from latex2word.source_graph import Finding, SourceLocation


def test_output_is_replaced_atomically_and_never_left_half_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "new.docx"
    source.write_bytes(b"NEW" * 1000)
    destination = tmp_path / "out" / "paper.docx"
    build._atomic_copy(source, destination)
    assert destination.read_bytes() == b"NEW" * 1000
    destination.write_bytes(b"OLD")

    def failing_copy(src, dst, *args, **kwargs):
        Path(dst).write_bytes(b"PART")  # a partial copy lands in the temporary file only
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copyfile", failing_copy)
    with pytest.raises(OSError, match="disk full"):
        build._atomic_copy(source, destination)
    assert destination.read_bytes() == b"OLD"
    assert [p.name for p in destination.parent.iterdir()] == ["paper.docx"]


def test_lua_empty_tables_arrive_as_objects_and_are_normalised_to_lists():
    report = build._normalise_report(
        {"labels": {}, "refs": [{"label": "a"}], "sequences": {"figures": {}, "tables": [{"n": 1}]}, "bibliography": {"manual_entries": {}}}
    )
    assert report["labels"] == [] and report["refs"] == [{"label": "a"}] and report["citations"] == []
    assert report["sequences"] == {"figures": [], "tables": [{"n": 1}], "equations": [], "sections": []}
    assert report["bibliography"]["manual_entries"] == []


def test_findings_restating_a_located_source_scan_finding_are_dropped_once():
    located = Finding("L2W-E-REF-001", Severity.ERROR, 'reference to undefined label "x" (\\ref)', SourceLocation("main.tex", 3))
    from_filter = Finding("L2W-E-REF-001", Severity.ERROR, 'reference to undefined label "x"')
    other = Finding("L2W-E-REF-001", Severity.ERROR, 'reference to undefined label "y"')
    assert build._dedupe([from_filter, located, other, other]) == [located, other]


@pytest.mark.parametrize(
    ("raw", "code", "severity"),
    [
        ("\\citeauthoryear{k}", "L2W-E-CITE-002", Severity.ERROR),
        ("\\cpageref{x}", "L2W-E-REF-006", Severity.ERROR),
        ("\\setlength{\\parindent}{0pt}", "L2W-W-TEX-005", Severity.WARNING),
    ],
)
def test_raw_tex_left_by_the_filter_is_classified_by_what_would_be_lost(raw: str, code: str, severity: Severity):
    finding = build._classify_raw({"kind": "inline", "text": raw})
    assert (finding.code, finding.severity) == (code, severity) and raw in finding.message
