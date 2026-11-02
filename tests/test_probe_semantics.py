"""F-05 follow-up (phase 2): a probe that cannot run is a tool error, not a verdict on the Pandoc version;
the pandoc-api-version pin is the PVP major series."""

from __future__ import annotations

import pytest

from latex2word.pandoc_compat import (
    PINNED_API_VERSIONS, PandocAssessment, PandocCompat, ProbeCheck, assess_pandoc, compat_findings, run_probe,
)
from latex2word.process import CommandResult


def _runner(**outcome):
    def run(args, *, timeout, **_):
        return CommandResult(tuple(map(str, args)), outcome.get("returncode"), outcome.get("stdout", ""), "",
                             outcome.get("timed_out", False), 0.1, outcome.get("error"))
    return run


@pytest.mark.parametrize(
    ("label", "outcome"),
    [
        ("timeout", {"timed_out": True}),
        ("launch-error", {"error": "[Errno 2] No such file or directory"}),
        ("killed-by-signal", {"returncode": -9}),
    ],
)
def test_probe_execution_failures_are_probe_errors(label, outcome):
    assessment = assess_pandoc("3.12", path="/x/pandoc", runner=_runner(**outcome), timeout_s=1, probe=True)
    assert assessment.compat is PandocCompat.PROBE_ERROR, label
    [finding] = compat_findings(assessment)
    assert finding.code == "L2W-E-PANDOC-004" and "not a verdict" in finding.message
    assert "UNSUPPORTED" not in finding.message


def test_a_pandoc_that_runs_and_fails_is_an_observed_incompatibility():
    assessment = assess_pandoc("3.12", path="/x/pandoc", runner=_runner(returncode=1), timeout_s=1, probe=True)
    assert assessment.compat is PandocCompat.UNSUPPORTED
    assert compat_findings(assessment)[0].code == "L2W-E-PANDOC-003"


def test_malformed_output_with_exit_0_is_an_observed_incompatibility():
    assessment = assess_pandoc("3.12", path="/x/pandoc", runner=_runner(returncode=0, stdout="not json"), timeout_s=1, probe=True)
    assert assessment.compat is PandocCompat.UNSUPPORTED


def test_observed_incompatibility_wins_over_a_step_that_did_not_run(monkeypatch):
    checks = (ProbeCheck("latex-reader", False, "AST differs"), ProbeCheck("citeproc", False, "timed out", True))
    monkeypatch.setattr("latex2word.pandoc_compat.run_probe", lambda path, runner, timeout_s: checks)
    assert assess_pandoc("3.12", path="/x", runner=None, timeout_s=1, probe=True).compat is PandocCompat.UNSUPPORTED


def test_probe_setup_failure_is_a_probe_error(monkeypatch):
    def broken(path, runner, timeout_s):
        raise OSError("No space left on device")
    monkeypatch.setattr("latex2word.pandoc_compat.run_probe", broken)
    assessment = assess_pandoc("3.12", path="/x", runner=None, timeout_s=1, probe=True)
    assert assessment.compat is PandocCompat.PROBE_ERROR and "No space left" in assessment.reasons[0]


def test_version_floor_stays_an_incompatibility_without_probing():
    assert assess_pandoc("2.19", path="/x", runner=None, timeout_s=1, probe=True).compat is PandocCompat.UNSUPPORTED


def test_build_refuses_a_probe_error_with_the_probe_message(tmp_path, monkeypatch):
    import yaml
    import latex2word.build as build_module
    from latex2word.config import load_config
    from latex2word.errors import BuildError
    from latex2word.pandoc_adapter import PandocTool

    root = tmp_path / "p"; root.mkdir(); (root / "main.tex").write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")
    (tmp_path / "c.yaml").write_text(yaml.safe_dump({"schema_version": 1, "project": {"name": "p", "root": str(root), "main": "main.tex"},
                                                     "profile": "generic", "workspace": {"root": str(tmp_path / "ws")}}))
    monkeypatch.setattr(build_module, "locate_pandoc", lambda **_: PandocTool("/fake/pandoc", "3.12", "pandoc 3.12"))
    monkeypatch.setattr(build_module, "assess_pandoc", lambda version, **_: PandocAssessment(PandocCompat.PROBE_ERROR, version, (), ("latex-reader: timed out",)))
    with pytest.raises(BuildError, match="L2W-E-PANDOC-004"):
        build_module.run_build(load_config(tmp_path / "c.yaml"))


def test_doctor_reports_a_probe_error_as_untested_not_incompatible(monkeypatch):
    from latex2word.diagnostics import CapabilityState
    from latex2word.toolchain import _pandoc_capabilities
    from latex2word.diagnostics import Status

    class Diag:
        status, message, path = Status.PASS, "", "/x/pandoc"
    assessment = PandocAssessment(PandocCompat.PROBE_ERROR, "3.12", (ProbeCheck("latex-reader", False, "timed out", True),), ("timed out",))
    caps = {c.name: c.state for c in _pandoc_capabilities(Diag(), assessment)}
    assert caps["compatibility"] is CapabilityState.UNTESTED and caps["latex-reader"] is CapabilityState.UNTESTED


def _api_ok(api: tuple[int, ...]) -> bool:
    return any(api[: len(pin)] == pin for pin in PINNED_API_VERSIONS)


@pytest.mark.parametrize(
    ("api", "accepted"),
    [((1, 23), True), ((1, 23, 1), True), ((1, 23, 1, 2), True), ((1, 23, 2), True), ((1, 23, 9, 1), True),
     ((1, 22, 2, 1), False), ((1, 24), False), ((1, 24, 0), False), ((2, 23), False), ((1, 2, 3), False)],
)
def test_api_version_rule_is_the_pvp_major_series(api, accepted):
    assert _api_ok(api) is accepted


def test_real_probe_against_the_installed_pandoc_if_any():
    import shutil
    from latex2word.process import run_command
    path = shutil.which("pandoc")
    if path is None:
        pytest.skip("pandoc not installed")
    checks = run_probe(path, runner=run_command, timeout_s=60)
    assert not any(c.execution_error for c in checks)  # whatever the verdict, the installed Pandoc ran every step
