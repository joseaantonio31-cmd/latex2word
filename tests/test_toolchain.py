from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ALL_TOOLS, FakeRunner, certified_pandoc_runner, fake_which, make_result

from latex2word.diagnostics import Role, Status, render_json, render_text
from latex2word.errors import ExitCode
from latex2word.profiles import load_profile
from latex2word.toolchain import (
    TOOL_SPECS,
    ToolSpec,
    check_tool,
    find_dist_version,
    inspect_toolchain,
)

PANDOC = next(s for s in TOOL_SPECS if s.name == "pandoc")
INKSCAPE = next(s for s in TOOL_SPECS if s.name == "inkscape")
NUMBERING = next(s for s in TOOL_SPECS if s.name == "pandoc-tex-numbering")


def by_name(report):
    return {d.subsystem: d for d in report.diagnostics}


def inspect(present, runner=None, **kwargs):
    """Doctor over a fake PATH with a certified Pandoc; the real-process capability probe is off unless asked for."""
    kwargs.setdefault("probe", False)
    return inspect_toolchain(
        which=fake_which(present), runner=runner or certified_pandoc_runner(), interpreter="/fake/bin/python", **kwargs
    )


def test_required_tool_found_is_pass_with_version_and_path():
    diagnostic = check_tool(PANDOC, which=fake_which({"pandoc"}), runner=FakeRunner(
        {"/fake/bin/pandoc": make_result(stdout="pandoc 3.12\nFeatures: +server\n")}
    ))
    assert diagnostic.status is Status.PASS
    assert diagnostic.version == "pandoc 3.12"
    assert diagnostic.path == "/fake/bin/pandoc"


def test_version_falls_back_to_stderr():
    runner = FakeRunner({"/fake/bin/pandoc": make_result(stdout="", stderr="pandoc 3.12\n")})
    assert check_tool(PANDOC, which=fake_which({"pandoc"}), runner=runner).version == "pandoc 3.12"


def test_missing_required_tool_is_fail_and_not_ready():
    report = inspect(ALL_TOOLS - {"pandoc"})
    assert by_name(report)["pandoc"].status is Status.FAIL
    assert report.ready is False
    assert report.exit_code == ExitCode.TOOLS_UNAVAILABLE
    assert report.summary()["status"] == "NOT_READY"
    assert report.summary()["required_fail"] == 1


def test_missing_optional_tool_is_warn_and_still_ready():
    report = inspect(ALL_TOOLS - {"inkscape"})
    assert by_name(report)["inkscape"].status is Status.WARN
    assert report.ready is True
    assert report.exit_code == ExitCode.OK
    assert report.summary()["optional_warn"] == 1


@pytest.mark.parametrize(("spec", "expected"), [(PANDOC, Status.FAIL), (INKSCAPE, Status.WARN)])
def test_probe_timeout_is_reported_not_raised(spec: ToolSpec, expected: Status):
    runner = FakeRunner({f"/fake/bin/{spec.name}": make_result(returncode=None, timed_out=True, stdout="partial")})
    diagnostic = check_tool(spec, which=fake_which({spec.name}), runner=runner, timeout_s=7)
    assert diagnostic.status is expected
    assert "timed out after 7s" in diagnostic.message
    assert diagnostic.version is None


@pytest.mark.parametrize(("spec", "expected"), [(PANDOC, Status.FAIL), (INKSCAPE, Status.WARN)])
def test_nonzero_version_probe_is_unusable(spec: ToolSpec, expected: Status):
    runner = FakeRunner({f"/fake/bin/{spec.name}": make_result(returncode=3, stderr="boom\n")})
    diagnostic = check_tool(spec, which=fake_which({spec.name}), runner=runner)
    assert diagnostic.status is expected
    assert "exited with code 3" in diagnostic.message
    assert diagnostic.detail == "boom"
    assert diagnostic.path == f"/fake/bin/{spec.name}"


def test_unlaunchable_tool_is_unusable():
    runner = FakeRunner({"/fake/bin/pandoc": make_result(returncode=None, error="[WinError 193] not a valid Win32 app")})
    diagnostic = check_tool(PANDOC, which=fake_which({"pandoc"}), runner=runner)
    assert diagnostic.status is Status.FAIL
    assert "could not be executed" in diagnostic.message


def test_empty_version_output_is_pass_without_version():
    runner = FakeRunner({"/fake/bin/pandoc": make_result(stdout="\n", stderr="")})
    diagnostic = check_tool(PANDOC, which=fake_which({"pandoc"}), runner=runner)
    assert diagnostic.status is Status.PASS
    assert diagnostic.version is None
    assert diagnostic.message == "present, version unavailable"


def test_running_interpreter_is_used_for_python_not_path_lookup():
    runner = FakeRunner()
    report = inspect(set(), runner=runner)  # nothing on PATH
    assert by_name(report)["python"].status is Status.PASS
    assert by_name(report)["python"].path == "/fake/bin/python"


def test_pandoc_tex_numbering_absent_does_not_break_doctor():
    report = inspect(ALL_TOOLS - {"pandoc-tex-numbering"})
    assert by_name(report)["pandoc-tex-numbering"].status is Status.WARN
    assert report.ready is True


def test_pandoc_tex_numbering_is_never_executed():
    runner = FakeRunner()
    report = inspect(ALL_TOOLS, runner=runner)
    diagnostic = by_name(report)["pandoc-tex-numbering"]
    assert diagnostic.status is Status.PASS
    assert diagnostic.message == "present, version unavailable"
    assert diagnostic.version is None
    assert all("pandoc-tex-numbering" not in call[0] for call in runner.calls)


def make_environment(tmp_path: Path, version: str | None, *, layout: str) -> Path:
    prefix = tmp_path / "env"
    scripts = prefix / ("Scripts" if layout == "windows" else "bin")
    site = prefix / ("Lib" if layout == "windows" else "lib/python3.12") / "site-packages"
    scripts.mkdir(parents=True)
    site.mkdir(parents=True)
    exe = scripts / "pandoc-tex-numbering"
    exe.write_text("", encoding="utf-8")
    if version is not None:
        dist = site / f"pandoc_tex_numbering-{version}.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(
            f"Metadata-Version: 2.5\nName: pandoc-tex-numbering\nVersion: {version}\n\nVersion: 9.9.9\n",
            encoding="utf-8",
        )
    return exe


@pytest.mark.parametrize("layout", ["windows", "posix"])
def test_find_dist_version_reads_metadata_headers_only(tmp_path: Path, layout: str):
    exe = make_environment(tmp_path, "1.3.4", layout=layout)
    assert find_dist_version(exe, "pandoc-tex-numbering") == "1.3.4"


def test_find_dist_version_unknown_when_no_metadata(tmp_path: Path):
    exe = make_environment(tmp_path, None, layout="windows")
    assert find_dist_version(exe, "pandoc-tex-numbering") is None


def test_find_dist_version_unknown_when_ambiguous(tmp_path: Path):
    exe = make_environment(tmp_path, "1.3.4", layout="windows")
    other = exe.parent.parent / "Lib" / "site-packages" / "pandoc_tex_numbering-1.0.dist-info"
    other.mkdir()
    (other / "METADATA").write_text("Version: 1.0\n", encoding="utf-8")
    assert find_dist_version(exe, "pandoc-tex-numbering") is None


def test_pandoc_tex_numbering_version_from_metadata_without_running_it(tmp_path: Path):
    exe = make_environment(tmp_path, "1.3.4", layout="posix")
    runner = FakeRunner()
    diagnostic = check_tool(NUMBERING, which=fake_which({"pandoc-tex-numbering": str(exe)}), runner=runner)
    assert (diagnostic.status, diagnostic.version) == (Status.PASS, "1.3.4")
    assert runner.calls == []


def test_powershell_probed_on_windows_only_and_never_required():
    windows = by_name(inspect({"powershell"} | ALL_TOOLS, platform="win32"))
    assert windows["powershell"].role is Role.INFORMATIONAL
    assert windows["pwsh"].status is Status.SKIP

    linux = by_name(inspect(ALL_TOOLS, platform="linux"))
    assert "powershell" not in linux and "pwsh" not in linux


def test_missing_powershell_does_not_affect_readiness():
    assert inspect(ALL_TOOLS, platform="win32").ready is True


def test_json_report_schema_and_stability():
    report = inspect(ALL_TOOLS - {"inkscape"})
    data = json.loads(render_json(report))
    assert list(data)[:8] == [
        "schema", "status", "required_pass", "required_fail",
        "optional_pass", "optional_warn", "informational_pass", "informational_skip",
    ]
    assert data["status"] == "READY"
    assert (data["required_pass"], data["required_fail"], data["optional_warn"]) == (4, 0, 1)
    assert {t["subsystem"] for t in data["tools"]} >= ALL_TOOLS | {"python"}
    assert set(data["tools"][0]) == {
        "subsystem", "role", "status", "severity", "message", "path", "version", "detail"
    }
    assert render_json(report) == render_json(report)


def test_text_rendering_is_concise_and_verbose_adds_detail():
    report = inspect(ALL_TOOLS, platform="linux")
    text = render_text(report)
    assert text.endswith("Result: READY (required: 4 pass, 0 fail; optional: 5 pass, 0 warn)")
    assert "present, version unavailable" in text
    assert "executable not run" not in text
    assert "executable not run" in render_text(report, verbose=True)


def test_registry_matches_ieee_profile_metadata():
    profile = load_profile("ieee-conference")
    registry = {s.name: s.role for s in TOOL_SPECS}
    assert profile.id == "ieee-conference"
    assert all(registry[name] is Role.REQUIRED for name in profile.required_tools)
    assert all(registry[name] is Role.OPTIONAL for name in profile.optional_tools)
    assert {n for n, r in registry.items() if r is Role.REQUIRED} == set(profile.required_tools) | {"python"}
