from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pandockit import requires_pandoc
from test_build import FIXTURES, setup_project

from latex2word.cli import main


def run(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@requires_pandoc
def test_build_json_reports_numbering_diagnostics_and_output(tmp_path: Path, capsys):
    _, config = setup_project(tmp_path, "generic_article")
    code, out, _ = run(capsys, "build", str(config.config_file), "--json")
    data = json.loads(out)
    assert code == 0 and data["status"] == "OK" and data["profile"] == "generic"
    assert data["summary"]["numbering"]["tables"] == ["Table 1", "Table 2"]
    assert Path(data["output"]).is_file() and Path(data["manifest"]).is_file()
    assert all(d["severity"] != "info" for d in data["diagnostics"])


@requires_pandoc
def test_build_text_output_lists_the_semantic_sequences(tmp_path: Path, capsys):
    _, config = setup_project(tmp_path, "generic_article")
    code, out, err = run(capsys, "build", str(config.config_file))
    assert code == 0 and "Result: OK" in out
    assert "Figures:   Figure 1, Figure 2" in out and "Equations: (1), (2)" in out
    assert "Unnumbered headings: Data Availability, Supplementary Material" in out
    assert "running Pandoc" in err  # progress goes to stderr, results to stdout


@requires_pandoc
def test_build_output_flag_and_quiet_mode(tmp_path: Path, capsys):
    _, config = setup_project(tmp_path, "generic_article")
    target = tmp_path / "custom" / "paper.docx"
    code, out, err = run(capsys, "build", str(config.config_file), "--output", str(target), "-q")
    assert code == 0 and out.strip() == "OK" and target.is_file() and err == ""


@requires_pandoc
def test_failed_build_exits_with_the_problems_code_and_shows_the_diagnostic(tmp_path: Path, capsys):
    main_tex = (FIXTURES / "generic_article" / "main.tex").read_text(encoding="utf-8").replace("\\cite{alpha}", "\\cite{ghost}")
    _, config = setup_project(tmp_path, "generic_article", edit={"main.tex": main_tex})
    code, out, _ = run(capsys, "build", str(config.config_file))
    assert code == 1 and "Result: FAILED" in out and "L2W-E-CITE-001" in out and "'ghost'" in out
    code, out, _ = run(capsys, "build", str(config.config_file), "--json")
    data = json.loads(out)
    assert code == 1 and data["status"] == "FAILED" and data["output"] is None
    errors = [d["code"] for d in data["diagnostics"] if d["severity"] == "error"]
    assert errors[0] == "L2W-E-CITE-001" and set(errors) <= {"L2W-E-CITE-001", "L2W-E-CITE-010"}  # 010: the DOCX shows "(ghost?)"


def test_unknown_profile_is_reported_as_a_configuration_error(tmp_path: Path, capsys):
    _, config = setup_project(tmp_path, "generic_article", profile="no-such-profile")
    code, _, err = run(capsys, "build", str(config.config_file))
    assert code == 2 and "unknown profile 'no-such-profile'" in err
    code, out, _ = run(capsys, "build", str(config.config_file), "--json")
    assert code == 2 and json.loads(out)["valid"] is False


def test_missing_configuration_file_is_an_error(tmp_path: Path, capsys):
    code, _, err = run(capsys, "build", str(tmp_path / "absent.yaml"))
    assert code == 2 and "cannot read" in err


def test_module_entry_point_has_the_build_command():
    completed = subprocess.run([sys.executable, "-m", "latex2word", "build", "--help"], capture_output=True, text=True, check=True)
    assert "--keep-workspace" in completed.stdout and "--output" in completed.stdout


def test_build_with_pandoc_missing_exits_2(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch):
    _, config = setup_project(tmp_path, "generic_article")
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    code, _, err = run(capsys, "build", str(config.config_file))
    assert code == 2 and "pandoc" in err
