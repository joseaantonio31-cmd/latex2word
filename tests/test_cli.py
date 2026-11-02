from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import ALL_TOOLS, REPO_ROOT, certified_pandoc_runner, fake_which

from latex2word import __version__, cli
from latex2word.toolchain import inspect_toolchain

EXAMPLE = str(REPO_ROOT / "examples" / "minimal-project.yaml")


@pytest.fixture
def fake_toolchain(monkeypatch: pytest.MonkeyPatch):
    """Route ``doctor`` through the real logic with a fake PATH; returns a setter."""

    def install(present: set[str]) -> None:
        def patched(**kwargs):
            # a certified Pandoc on the fake PATH; the real-process capability probe is tested separately
            return inspect_toolchain(
                which=fake_which(present), runner=certified_pandoc_runner(), interpreter="/fake/bin/python",
                probe=False, **kwargs
            )

        monkeypatch.setattr(cli, "inspect_toolchain", patched)

    install(ALL_TOOLS)
    return install


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_help_exits_zero(capsys):
    code, out, _ = run(capsys, "--help")
    assert code == 0
    assert "doctor" in out and "config" in out


def test_version_exits_zero(capsys):
    code, out, _ = run(capsys, "--version")
    assert code == 0
    assert out.strip() == f"latex2word {__version__}"


def test_no_command_is_a_usage_error(capsys):
    code, _, err = run(capsys)
    assert code == 2
    assert "usage:" in err


def test_build_requires_a_config_file(capsys):
    code, _, err = run(capsys, "build")
    assert code == 2
    assert "config" in err


def test_doctor_json_has_expected_top_level_schema(capsys, fake_toolchain):
    code, out, _ = run(capsys, "doctor", "--json")
    data = json.loads(out)
    assert code == 0
    assert data["schema"] == "latex2word.doctor/1"
    assert data["status"] == "READY"
    assert {"required_pass", "required_fail", "optional_pass", "optional_warn", "tools"} <= set(data)
    assert isinstance(data["tools"], list) and data["tools"]


def test_doctor_exit_code_1_when_required_tool_missing(capsys, fake_toolchain):
    fake_toolchain(ALL_TOOLS - {"dvisvgm"})
    code, out, _ = run(capsys, "doctor", "--json")
    assert code == 1
    assert json.loads(out)["status"] == "NOT_READY"


def test_doctor_exit_code_0_when_only_optional_missing(capsys, fake_toolchain):
    fake_toolchain(ALL_TOOLS - {"inkscape", "rsvg-convert", "pdftocairo", "pandoc-tex-numbering"})
    code, out, _ = run(capsys, "doctor")
    assert code == 0
    assert "[WARN] inkscape" in out


def test_doctor_quiet_prints_only_the_result_line(capsys, fake_toolchain):
    code, out, _ = run(capsys, "doctor", "--quiet")
    assert code == 0
    assert out.startswith("Result: READY") and out.count("\n") == 1


def test_doctor_rejects_non_positive_timeout(capsys):
    code, _, err = run(capsys, "doctor", "--timeout", "0")
    assert code == 2
    assert "greater than 0" in err


def test_quiet_and_verbose_are_exclusive(capsys, fake_toolchain):
    code, _, err = run(capsys, "doctor", "-q", "-v")
    assert code == 2
    assert "mutually exclusive" in err


def test_config_validate_example(capsys):
    code, out, _ = run(capsys, "config", "validate", EXAMPLE)
    assert code == 0
    assert out.startswith("OK:")


def test_config_validate_json(capsys):
    code, out, _ = run(capsys, "config", "validate", EXAMPLE, "--json")
    assert code == 0
    assert json.loads(out)["valid"] is True


def test_config_validate_failure_is_exit_2_without_traceback(capsys, tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 7\n", encoding="utf-8")
    code, out, err = run(capsys, "config", "validate", str(bad))
    assert code == 2
    assert out == ""
    assert "unsupported schema_version 7" in err
    assert "Traceback" not in err


def test_config_validate_failure_json_reports_errors_on_stdout(capsys, tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nprofile: p\n", encoding="utf-8")
    code, out, _ = run(capsys, "config", "validate", str(bad), "--json")
    data = json.loads(out)
    assert code == 2 and data["valid"] is False
    assert any("project" in e["message"] for e in data["errors"])


def test_config_validate_missing_file_is_exit_2(capsys, tmp_path: Path):
    code, _, err = run(capsys, "config", "validate", str(tmp_path / "nope.yaml"))
    assert code == 2 and "cannot read" in err


def test_config_validate_check_files_flag(capsys, tmp_path: Path):
    path = tmp_path / "c.yaml"
    path.write_text(Path(EXAMPLE).read_text(encoding="utf-8"), encoding="utf-8")
    assert run(capsys, "config", "validate", str(path))[0] == 0
    code, _, err = run(capsys, "config", "validate", str(path), "--check-files")
    assert code == 2 and "project.main" in err
    (tmp_path / "main.tex").write_text("x", encoding="utf-8")
    assert run(capsys, "config", "validate", str(path), "--check-files")[0] == 0


def test_config_show_json_is_resolved_and_stable(capsys):
    code, first, _ = run(capsys, "config", "show", EXAMPLE, "--json")
    _, second, _ = run(capsys, "config", "show", EXAMPLE, "--json")
    data = json.loads(first)
    assert code == 0 and first == second
    assert data["project"]["main"] == (REPO_ROOT / "examples" / "main.tex").resolve().as_posix()
    assert data["workspace"]["root"].endswith("/examples/.latex2word")


def test_config_show_yaml_round_trips(capsys):
    code, out, _ = run(capsys, "config", "show", EXAMPLE)
    assert code == 0
    assert yaml.safe_load(out)["profile"] == "ieee-conference"


def test_module_entry_point_runs_in_a_subprocess():
    done = subprocess.run(
        [sys.executable, "-m", "latex2word", "--version"], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0
    assert done.stdout.strip() == f"latex2word {__version__}"


@pytest.mark.integration
def test_real_doctor_json_on_this_machine():
    done = subprocess.run(
        [sys.executable, "-m", "latex2word", "doctor", "--json"],
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert done.returncode in (0, 1), done.stderr
    data = json.loads(done.stdout)
    assert data["schema"] == "latex2word.doctor/1"
    assert data["status"] == ("READY" if done.returncode == 0 else "NOT_READY")
