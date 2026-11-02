from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT, fake_which
from projectkit import PNG_BYTES, ScriptedRunner, make_project, tree_digest, write_project_config
from test_figure_pipeline import ALL, tool_handler

from latex2word import cli, figure_pipeline

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "minimal_project" / "project.yaml"


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def fake_tools(monkeypatch: pytest.MonkeyPatch):
    """Route ``figures`` through the real pipeline with scripted external tools."""
    real = figure_pipeline.run_figures

    def install(*, present: set[str] = ALL, fail: set[str] = frozenset()):
        def patched(config, **kwargs):
            return real(config, which=fake_which(present), runner=ScriptedRunner(tool_handler(fail=fail)), **kwargs)

        monkeypatch.setattr(cli, "run_figures", patched)

    install()
    return install


def project(tmp_path: Path, files: dict[str, str | bytes] | None = None) -> Path:
    root = make_project(
        tmp_path,
        files
        or {
            "main.tex": "\\input{a}\\bibliography{refs}\n",
            "a.tex": "\\begin{figure}\\includegraphics{pic.png}\\label{fig:p}\\end{figure}\\begin{tikzpicture}\\end{tikzpicture}",
            "pic.png": PNG_BYTES,
            "refs.bib": "@a{k,}",
            "orphan.tex": "\\begin{circuitikz}\\end{circuitikz}",
        },
    )
    return write_project_config(tmp_path, root)


def test_new_commands_have_help(capsys):
    for command in ("inspect", "figures"):
        code, out, _ = run(capsys, command, "--help")
        assert code == 0 and "<config.yaml>" in out and "--json" in out
    assert "--previews" in run(capsys, "figures", "--help")[1]


def test_inspect_human_report(tmp_path: Path, capsys):
    code, out, _ = run(capsys, "inspect", str(project(tmp_path)))
    assert code == 0
    assert "main.tex" in out and "refs.bib" in out
    assert "Figures (2 active, 1 inactive)" in out and "[inactive] orphan" in out
    assert "type=png" in out and "type=tikzpicture" in out


def test_inspect_json_report(tmp_path: Path, capsys):
    code, out, _ = run(capsys, "inspect", str(project(tmp_path)), "--json")
    data = json.loads(out)
    assert code == 0 and data["schema"] == "latex2word.inspect/1"
    assert data["tex_files"] == ["main.tex", "a.tex"]
    assert data["bibliography_files"] == ["refs.bib"]
    assert data["summary"]["figures_active_by_type"] == {"png": 1, "tikzpicture": 1}
    assert [f["active"] for f in data["figures"]] == [True, True, False]
    assert data["missing_dependencies"] == [] and data["outside_root_dependencies"] == []
    assert {"nodes", "edges", "findings"} <= set(data["graph"])


def test_inspect_is_read_only_and_creates_no_workspace(tmp_path: Path, capsys):
    config = project(tmp_path)
    root = tmp_path / "project"
    before = tree_digest(root)
    run(capsys, "inspect", str(config))
    assert tree_digest(root) == before
    assert not (tmp_path / "workspace").exists()
    assert not (root / ".latex2word").exists()


def test_inspect_exit_one_for_missing_and_outside_dependencies(tmp_path: Path, capsys):
    config = project(tmp_path, {"main.tex": "\\input{gone}\\input{../outside}\\includegraphics{x.png}"})
    code, out, _ = run(capsys, "inspect", str(config))
    assert code == 1 and "Missing dependencies (2)" in out and "outside the project root (1)" in out
    code, out, _ = run(capsys, "inspect", str(config), "--json")
    data = json.loads(out)
    assert code == 1 and len(data["missing_dependencies"]) == 2
    assert [d["reference"] for d in data["outside_root_dependencies"]] == ["../outside"]


def test_inspect_exit_one_when_main_file_is_missing(tmp_path: Path, capsys):
    code, out, _ = run(capsys, "inspect", str(project(tmp_path, {"other.tex": ""})))
    assert code == 1 and "main-missing" in out


def test_inspect_exit_two_for_bad_configuration(tmp_path: Path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nproject: {name: x}\n", encoding="utf-8")
    code, _, err = run(capsys, "inspect", str(bad))
    assert code == 2 and "main" in err
    code, out, _ = run(capsys, "inspect", str(bad), "--json")
    assert code == 2 and json.loads(out)["valid"] is False
    assert run(capsys, "inspect", str(tmp_path / "missing.yaml"))[0] == 2


def test_inspect_runs_on_the_bundled_fixture(capsys):
    code, out, _ = run(capsys, "inspect", str(FIXTURE))
    assert code == 0 and "sections/intro.tex" in out and "1 active" in out


def test_inspect_module_entry_point_matches_cli(tmp_path: Path):
    completed = subprocess.run(
        [sys.executable, "-m", "latex2word", "inspect", str(FIXTURE), "--json"],
        capture_output=True, text=True, check=False, cwd=tmp_path,
    )
    assert completed.returncode == 0 and json.loads(completed.stdout)["summary"]["status"] == "OK"


def test_figures_human_report_and_manifest(tmp_path: Path, capsys, fake_tools):
    config = project(tmp_path)
    code, out, _ = run(capsys, "figures", str(config))
    assert code == 0
    assert "[ok] a  type=tikzpicture  renderer=latex-dvisvgm" in out
    assert "type=png  renderer=copy" in out and "Inactive (not rendered): orphan" in out
    assert (tmp_path / "workspace" / "figures" / "figure_manifest.json").is_file()
    assert "Result: OK (2/2 rendered)" in out


def test_figures_json_report(tmp_path: Path, capsys, fake_tools):
    code, out, _ = run(capsys, "figures", str(project(tmp_path)), "--json", "--previews")
    data = json.loads(out)
    assert code == 0 and data["summary"]["status"] == "OK" and data["summary"]["rendered"] == 2
    assert data["manifest"] == "figures/figure_manifest.json"
    assert [f["status"] for f in data["figures"]] == ["ok", "ok"]
    assert data["inactive_figures"] == [{"id": "orphan", "source_file": "orphan.tex"}]


def test_figures_exit_one_when_a_figure_fails(tmp_path: Path, capsys, fake_tools):
    fake_tools(fail={"latex"})
    code, out, _ = run(capsys, "figures", str(project(tmp_path)))
    assert code == 1 and "[FAILED]" in out and "Result: PROBLEMS" in out
    code, out, _ = run(capsys, "figures", str(project(tmp_path)), "--json")
    assert code == 1 and json.loads(out)["summary"]["failed"] == 1


def test_figures_exit_one_on_dependency_errors_without_creating_a_workspace(tmp_path: Path, capsys, fake_tools):
    config = project(tmp_path, {"main.tex": "\\input{gone}"})
    code, out, _ = run(capsys, "figures", str(config))
    assert code == 1 and "dependency errors" in out and "missing-dependency" in out
    assert not (tmp_path / "workspace").exists()


def test_figures_exit_two_for_bad_configuration(tmp_path: Path, capsys, fake_tools):
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: [valid", encoding="utf-8")
    assert run(capsys, "figures", str(bad))[0] == 2
    assert run(capsys, "figures", str(bad), "--json")[0] == 2


def test_figures_unsafe_workspace_is_a_program_error(tmp_path: Path, capsys, fake_tools):
    config = project(tmp_path)
    foreign = tmp_path / "workspace"
    foreign.mkdir()
    (foreign / "mine.txt").write_text("keep", encoding="utf-8")
    code, _, err = run(capsys, "figures", str(config))
    assert code == 2 and "not a latex2word workspace" in err
    assert (foreign / "mine.txt").exists()
