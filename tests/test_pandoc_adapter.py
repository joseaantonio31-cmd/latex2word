from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import fake_which, make_result
from projectkit import ScriptedRunner

from latex2word.diagnostics import Severity
from latex2word.errors import BuildError
from latex2word.pandoc_adapter import (
    PandocOptions,
    PandocTool,
    build_argv,
    locate_pandoc,
    parse_warnings,
    run_pandoc,
)
from latex2word.process import CommandResult

TOOL = PandocTool("/fake/bin/pandoc", "3.12", "pandoc 3.12")


def options(**kwargs) -> PandocOptions:
    defaults = {"inputs": ["main.tex"], "output": Path("/ws/out.docx")}
    return PandocOptions(**{**defaults, **kwargs})


class Fake:
    def __init__(self, result: CommandResult | None = None) -> None:
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, args, *, timeout, cwd=None, env=None, input_text=None) -> CommandResult:
        self.calls.append({"args": tuple(args), "timeout": timeout, "cwd": cwd, "env": env})
        return replace(self.result or make_result(), args=tuple(str(a) for a in args))


def test_pandoc_is_resolved_through_the_toolchain_layer():
    runner = ScriptedRunner(lambda argv, cwd: make_result(argv, stdout="pandoc 3.12\nFeatures: +server +lua\n"))
    tool = locate_pandoc(which=fake_which({"pandoc": "/opt/pandoc"}), runner=runner)
    assert (tool.path, tool.version) == ("/opt/pandoc", "3.12")
    assert runner.calls[0][0] == ("/opt/pandoc", "--version")


def test_missing_or_broken_pandoc_is_a_build_error_not_a_crash():
    with pytest.raises(BuildError, match="not found"):
        locate_pandoc(which=fake_which(set()), runner=ScriptedRunner(lambda a, c: make_result(a)))
    broken = ScriptedRunner(lambda argv, cwd: make_result(argv, returncode=3, stderr="boom"))
    with pytest.raises(BuildError, match="exited with code 3"):
        locate_pandoc(which=fake_which({"pandoc"}), runner=broken)


def test_argv_is_an_argument_vector_with_filters_before_citeproc_and_input_last(tmp_path: Path):
    argv = build_argv(
        "/bin/pandoc",
        options(
            lua_filters=[Path("/f/one.lua"), Path("/f/two.lua")],
            metadata={"l2w-config": "/c.json", "reference-section-title": "References"},
            citeproc=True,
            bibliography=[Path("/b/a.bib"), Path("/b/b.bib")],
            csl=Path("/s/style.csl"),
            reference_doc=Path("/r/ref.docx"),
            resource_paths=[Path("/x"), Path("/y")],
            extra_args=["--dpi=300"],
        ),
    )
    assert argv[0] == "/bin/pandoc" and argv[-1] == "main.tex" and all(isinstance(a, str) for a in argv)
    assert argv[argv.index("--resource-path") + 1] == os.pathsep.join([str(Path("/x")), str(Path("/y"))])
    assert argv.index("--lua-filter") < argv.index("--citeproc")
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--lua-filter"] == [str(Path("/f/one.lua")), str(Path("/f/two.lua"))]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--bibliography"] == [str(Path("/b/a.bib")), str(Path("/b/b.bib"))]
    assert argv[argv.index("--csl") + 1] == str(Path("/s/style.csl"))
    assert argv[argv.index("--reference-doc") + 1] == str(Path("/r/ref.docx"))
    assert "--metadata" in argv and "l2w-config=/c.json" in argv and "--dpi=300" in argv
    assert argv[argv.index("--output") + 1] == str(Path("/ws/out.docx"))


def test_optional_arguments_are_absent_by_default():
    argv = build_argv("/bin/pandoc", options())
    assert argv == ["/bin/pandoc", "--from", "latex", "--to", "docx", "--standalone", "--output", str(Path("/ws/out.docx")), "main.tex"]


def test_run_uses_the_given_cwd_timeout_and_a_deterministic_environment(tmp_path: Path):
    fake = Fake(make_result(stdout="", stderr="[WARNING] Could not fetch resource x.png: replacing image with description\n"))
    run = run_pandoc(TOOL, options(), cwd=tmp_path, timeout_s=42, runner=fake)
    call = fake.calls[0]
    assert call["cwd"] == tmp_path and call["timeout"] == 42 and call["env"]["SOURCE_DATE_EPOCH"] == "0"
    assert run.run.cwd == str(tmp_path) and run.run.argv == call["args"] and run.ok
    assert run.to_dict()["pandoc_version"] == "3.12" and run.to_dict()["exit_code"] == 0
    assert [w.code for w in run.warnings] == ["L2W-E-FIG-007"]


@pytest.mark.parametrize(
    ("result", "needle"),
    [
        (make_result(returncode=64, stderr="Unknown option --nope\n"), "exit code 64"),
        (make_result(returncode=None, timed_out=True, stderr="partial"), "timed out"),
        (make_result(returncode=None, error="no such file"), "could not start"),
    ],
)
def test_failures_become_structured_findings_and_keep_stderr(tmp_path: Path, result: CommandResult, needle: str):
    run = run_pandoc(TOOL, options(), cwd=tmp_path, runner=Fake(result))
    finding = run.failure_finding()
    assert not run.ok and finding is not None and finding.code == "L2W-E-PANDOC-001" and needle in finding.message
    assert run.to_dict()["stderr"] == result.stderr


def test_warnings_are_parsed_classified_and_never_dropped():
    stderr = (
        "[WARNING] Could not convert TeX math \\begin{rcases} a \\end{rcases}, rendering as TeX:\r\n"
        "  \\begin{rcases} a \\end{rcases}\r\n         ^\r\n  unexpected \"r\"\r\n"
        "[WARNING] Citeproc: citation nokey not found\r\n"
        "[WARNING] Could not fetch resource missing.png: replacing image with description\r\n"
        "[WARNING] Could not determine image size for foo.svg\r\n"
        "[WARNING] Something entirely new\r\n"
        "[INFO] not a warning\r\n"
    )
    warnings = parse_warnings(stderr)
    assert [(w.code, w.severity) for w in warnings] == [
        ("L2W-E-MATH-002", Severity.ERROR),
        ("L2W-E-CITE-001", Severity.ERROR),
        ("L2W-E-FIG-007", Severity.ERROR),
        ("L2W-W-FIG-005", Severity.WARNING),
        ("L2W-W-PANDOC-001", Severity.WARNING),
    ]
    assert warnings[0].text.count("\n") == 3 and "unexpected" in warnings[0].text
    finding = warnings[0].to_finding()
    assert finding.severity is Severity.ERROR and "detail line" in finding.message
    assert warnings[4].to_finding().message == "pandoc: Something entirely new"


def test_severity_letter_matches_the_code_for_every_classifier():
    for stderr in ("[WARNING] Could not convert TeX math x", "[WARNING] Citeproc: citation k not found", "[WARNING] Skipped x"):
        for warning in parse_warnings(stderr):
            assert warning.code.split("-")[1] == {Severity.ERROR: "E", Severity.WARNING: "W"}[warning.severity]
