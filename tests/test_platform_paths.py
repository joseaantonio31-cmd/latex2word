"""Cross-platform path, link, tool-lookup and process hygiene.

Tests are labelled in ``docs/platform.md``: those that execute the real file system run on the
machine at hand (Windows junctions only on Windows); the ``PurePosixPath``/``PureWindowsPath``
and injected ``which`` tests simulate the other platform's semantics and never claim to run it.
"""

from __future__ import annotations

import ast
import ntpath
import os
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
import yaml
from conftest import PRIVATE_MARKERS, REPO_ROOT, fake_which
from projectkit import make_dir_link, make_project

from latex2word.config import load_config
from latex2word.errors import ConfigError, StagingError, WorkspaceSafetyError
from latex2word.process import run_command
from latex2word.staging import StagingManifest, staging_dir, stage_files
from latex2word.tikz_render import render_tikz
from latex2word.toolchain import inspect_toolchain
from latex2word.workspace import Workspace, is_link_like, portable_name_problem

SRC = REPO_ROOT / "src" / "latex2word"
WINDOWS = sys.platform == "win32"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path, {"main.tex": "x", "sub/a.tex": "a", "Chap.tex": "c"})


@pytest.fixture
def workspace(tmp_path: Path, project: Path) -> Workspace:
    return Workspace.create(tmp_path / "ws", project)


# --- staging path validation (host independent) ---------------------------------------------

UNSAFE = [
    pytest.param("../x.tex", id="parent"),
    pytest.param("sub/../../x.tex", id="parent-inside"),
    pytest.param("..\\x.tex", id="parent-backslash"),
    pytest.param("/etc/passwd", id="posix-absolute"),
    pytest.param("\\x.tex", id="rooted-backslash"),
    pytest.param("C:\\x.tex", id="drive-absolute"),
    pytest.param("C:/x.tex", id="drive-absolute-slash"),
    pytest.param("C:x.tex", id="drive-relative"),
    pytest.param("\\\\server\\share\\f.tex", id="unc"),
    pytest.param("//server/share/f.tex", id="unc-slashes"),
    pytest.param("\\\\?\\C:\\x.tex", id="extended-length"),
    pytest.param("", id="empty"),
    pytest.param("con.tex", id="reserved-with-extension"),
    pytest.param("sub/NUL", id="reserved-upper-nested"),
    pytest.param("Aux.txt", id="reserved-mixed-case"),
    pytest.param("sub/com1.png", id="com1"),
    pytest.param("lpt9", id="lpt9"),
    pytest.param("COM\u00b9.tex", id="superscript-com"),
    pytest.param("con .tex", id="reserved-trailing-space-in-stem"),
    pytest.param("a.tex.", id="trailing-dot"),
    pytest.param("sub /a.tex", id="directory-trailing-space"),
    pytest.param("a.tex ", id="trailing-space"),
    pytest.param("a.tex:stream", id="alternate-data-stream"),
    pytest.param("a*b.tex", id="wildcard"),
    pytest.param("a\x00b.tex", id="nul-character"),
    pytest.param("a\nb.tex", id="control-character"),
]


@pytest.mark.parametrize("rel", UNSAFE)
def test_unsafe_staging_paths_are_refused_before_anything_is_written(
    rel: str, project: Path, workspace: Workspace, tmp_path: Path
):
    with pytest.raises(StagingError):
        stage_files(project, [("main.tex", "main"), (rel, "extra")], workspace)
    outside = [p for p in tmp_path.rglob("*") if p.is_file() and project not in p.parents and workspace.path not in p.parents]
    assert outside == []
    assert not (staging_dir(workspace) / "main.tex").exists() or (staging_dir(workspace) / "main.tex").read_bytes() == b"x"


def test_windows_absolute_forms_look_relative_to_posix_so_the_check_cannot_depend_on_the_host():
    # On Linux/macOS pathlib sees these as a single harmless relative segment...
    for rel in ("C:\\x.tex", "\\\\server\\share\\f.tex", "C:x.tex"):
        assert not PurePosixPath(rel).is_absolute()
        assert PurePosixPath(rel).parts[0] == rel or ".." not in PurePosixPath(rel).parts
    # ...while Windows sees an anchor; both verdicts must be "refuse".
    for rel in ("C:\\x.tex", "\\\\server\\share\\f.tex", "C:x.tex"):
        assert PureWindowsPath(rel).anchor
        assert portable_name_problem(PurePosixPath(rel).parts[0]) is not None or rel.startswith("\\\\")


@pytest.mark.parametrize(
    "rel",
    ["sub/com10.tex", "console.tex", "auxiliary/x.tex", "sub/.hidden", "my file.tex", "con-x.tex", "a.b.c.tex", "sub\\a.tex"],
)
def test_ordinary_names_close_to_reserved_ones_are_accepted(rel: str, tmp_path: Path):
    project = make_project(tmp_path, {"main.tex": "m", rel.replace("\\", "/"): "data"})
    workspace = Workspace.create(tmp_path / "ws", project)
    manifest = stage_files(project, [(rel, "extra")], workspace)
    assert manifest.files[0].source == rel.replace("\\", "/")


def test_backslash_separated_entries_are_normalised_to_posix(project: Path, workspace: Workspace):
    manifest = stage_files(project, [("sub\\a.tex", "include")], workspace)
    assert manifest.files[0].source == manifest.files[0].staged == "sub/a.tex"
    assert (staging_dir(workspace) / "sub" / "a.tex").read_bytes() == b"a"


def test_portable_name_problem_covers_each_rule():
    assert portable_name_problem("report.tex") is None
    assert portable_name_problem("com0.tex") is None
    assert portable_name_problem("PRN.") is not None
    assert portable_name_problem("nul.tar.gz") is not None
    assert portable_name_problem("CONOUT$") is not None
    assert portable_name_problem("a|b") is not None


def test_reserved_workspace_directory_name_is_refused(tmp_path: Path, project: Path):
    with pytest.raises(WorkspaceSafetyError, match="reserved"):
        Workspace.create(tmp_path / "aux", project)
    assert not (tmp_path / "aux").exists()


# --- case handling in staging ----------------------------------------------------------------


def _filesystem_is_case_sensitive(directory: Path) -> bool:
    (directory / "probe-a").write_bytes(b"")
    try:
        return not (directory / "PROBE-A").exists()
    finally:
        (directory / "probe-a").unlink()


def test_two_spellings_of_one_file_are_staged_once(tmp_path: Path, project: Path, workspace: Workspace):
    if _filesystem_is_case_sensitive(tmp_path):
        pytest.skip("needs a case-insensitive file system (Windows/macOS default)")
    manifest = stage_files(project, [("Chap.tex", "include"), ("chap.tex", "include")], workspace)
    assert [(f.source, f.staged) for f in manifest.files] == [("Chap.tex", "Chap.tex"), ("chap.tex", "Chap.tex")]
    staged = [p.name for p in staging_dir(workspace).iterdir()]
    assert staged == ["Chap.tex"]


def test_distinct_files_whose_names_differ_only_in_case_are_refused(
    monkeypatch: pytest.MonkeyPatch, project: Path, workspace: Workspace
):
    # Simulates a case-sensitive source tree holding two real files; the machine's own FS may be
    # case-insensitive, so the "different files" verdict is injected. On a case-sensitive FS the second spelling is
    # a file of its own and must exist (audit F-09: the test used to assume a case-insensitive host).
    if _filesystem_is_case_sensitive(project):
        (project / "chap.tex").write_bytes(b"other")
    monkeypatch.setattr(os.path, "samefile", lambda a, b: False)
    with pytest.raises(StagingError, match="letter case"):
        stage_files(project, [("Chap.tex", "include"), ("chap.tex", "include")], workspace)


def test_case_colliding_real_files_on_a_case_sensitive_filesystem(tmp_path: Path):
    root = tmp_path / "cs"
    root.mkdir()
    if not _filesystem_is_case_sensitive(tmp_path):
        pytest.skip("needs a case-sensitive file system")
    project = make_project(tmp_path, {"main.tex": "m", "Fig.tex": "1", "fig.tex": "2"}, name="p2")
    workspace = Workspace.create(tmp_path / "ws2", project)
    with pytest.raises(StagingError, match="letter case"):
        stage_files(project, [("Fig.tex", "include"), ("fig.tex", "include")], workspace)


def test_staging_failure_while_copying_is_a_staging_error(monkeypatch: pytest.MonkeyPatch, project: Path, workspace: Workspace):
    def deny(source: object, destination: object) -> None:
        raise PermissionError(13, "access denied")

    monkeypatch.setattr("latex2word.staging.shutil.copyfile", deny)
    with pytest.raises(StagingError, match="cannot stage main.tex"):
        stage_files(project, [("main.tex", "main")], workspace)


def test_manifest_alias_roundtrip_verifies(project: Path, workspace: Workspace, tmp_path: Path):
    if _filesystem_is_case_sensitive(tmp_path):
        pytest.skip("needs a case-insensitive file system")
    from latex2word.staging import verify_staging

    manifest = stage_files(project, [("Chap.tex", "include"), ("CHAP.tex", "include")], workspace)
    assert isinstance(manifest, StagingManifest)
    verify_staging(project, manifest, workspace)


# --- links -----------------------------------------------------------------------------------


def test_junction_or_directory_symlink_is_link_like_and_plain_directory_is_not(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    if not make_dir_link(link, target):
        pytest.skip("cannot create a directory symlink/junction here")
    assert is_link_like(link)
    assert not is_link_like(target)
    assert not is_link_like(tmp_path / "missing")


def test_file_symlink_is_link_like(tmp_path: Path):
    real = tmp_path / "real.txt"
    real.write_text("x")
    link = tmp_path / "file-link.txt"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("creating file symlinks is not permitted here")
    assert is_link_like(link)
    assert not is_link_like(real)


def test_linked_workspace_is_refused(tmp_path: Path, project: Path):
    real = tmp_path / "real-ws"
    Workspace.create(real, project)
    link = tmp_path / "ws-link"
    if not make_dir_link(link, real):
        pytest.skip("cannot create a directory symlink/junction here")
    with pytest.raises(WorkspaceSafetyError, match="symlink or junction"):
        Workspace.create(link, project)


def test_staging_directory_replaced_by_a_link_is_refused_and_target_survives(tmp_path: Path, project: Path, workspace: Workspace):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    if not make_dir_link(staging_dir(workspace), victim):
        pytest.skip("cannot create a directory symlink/junction here")
    with pytest.raises(StagingError, match="symlink or junction"):
        stage_files(project, [("main.tex", "main")], workspace)
    assert (victim / "keep.txt").read_text() == "keep"


def test_project_subdirectory_linked_outside_the_root_is_never_staged(tmp_path: Path, project: Path, workspace: Workspace):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.tex").write_text("secret")
    if not make_dir_link(project / "linked", outside):
        pytest.skip("cannot create a directory symlink/junction here")
    with pytest.raises(StagingError, match="symlink or junction"):
        stage_files(project, [("linked/secret.tex", "include")], workspace)


# --- tool lookup without PATHEXT assumptions --------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.argv: list[tuple[str, ...]] = []

    def __call__(self, args: tuple[str, ...], *, timeout: float):  # noqa: ANN204
        from conftest import make_result

        self.argv.append(tuple(args))
        return make_result(tuple(args), stdout="tool 1.0\n")


@pytest.mark.parametrize(
    "path_for",
    [
        pytest.param(lambda name: f"/usr/local/bin/{name}", id="posix-no-extension"),
        pytest.param(lambda name: f"C:\\Program Files\\Tools\\{name}.exe", id="windows-exe"),
        pytest.param(lambda name: f"C:\\tools\\shims\\{name}.cmd", id="windows-cmd-shim"),
    ],
)
def test_lookup_asks_for_bare_names_and_executes_exactly_what_which_returned(path_for):
    asked: list[str] = []

    def which(name: str) -> str | None:
        asked.append(name)
        return path_for(name)

    recorder = _Recorder()
    report = inspect_toolchain(which=which, runner=recorder, platform="linux")
    assert asked and not [n for n in asked if os.path.splitext(n)[1] or "/" in n or "\\" in n]
    executed = {argv[0] for argv in recorder.argv}
    # ntpath.basename splits on both "\\" and "/", so Windows paths are taken apart with Windows semantics on any
    # host (audit F-09: os.path.basename on POSIX does not split C:\\...\\pandoc.exe)
    assert executed and all(argv0 == path_for(ntpath.basename(argv0).split(".")[0]) for argv0 in executed
                            if argv0 != sys.executable)
    assert report.diagnostics


def test_windows_only_tools_are_not_looked_up_elsewhere():
    asked: list[str] = []
    inspect_toolchain(which=lambda n: asked.append(n), runner=_Recorder(), platform="linux")  # type: ignore[arg-type,return-value]
    assert "powershell" not in asked and "pwsh" not in asked


def test_render_runs_the_returned_tool_path_in_the_caller_owned_directory(tmp_path: Path):
    calls: list[tuple[tuple[str, ...], Path | None]] = []

    def runner(args, *, timeout, cwd=None, env=None, input_text=None):  # noqa: ANN001, ANN202
        from conftest import make_result

        calls.append((tuple(str(a) for a in args), cwd))
        return make_result(tuple(str(a) for a in args), returncode=1)

    workdir = tmp_path / "render"
    render_tikz(
        "fig", "\\documentclass{standalone}", workdir, tmp_path / "out.svg", timeout=5,
        which=fake_which({"latex": "/opt/tex/bin/latex", "dvisvgm": "/opt/tex/bin/dvisvgm"}), runner=runner,
    )
    assert calls[0][0][0] == "/opt/tex/bin/latex"
    assert calls[0][1] == workdir
    assert calls[0][0][-1] == "fig.tex"  # relative to cwd: no absolute or backslash paths in the tool's argv tail


# --- no shell, no PowerShell, argv arrays -------------------------------------------------------

_SHELL_FREE_ALLOWED_SUBPROCESS = {SRC / "process.py", SRC / "reference_doc.py"}
# toolchain.py *reports* PowerShell as an informational, Windows-only doctor line; it is never run for work.
_MAY_NAME_POWERSHELL = {SRC / "toolchain.py"}


def _module_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_production_code_never_uses_a_shell():
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(_module_ast(path)):
            where = f"{path.relative_to(SRC).as_posix()}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.keyword) and node.arg == "shell" and not (
                isinstance(node.value, ast.Constant) and node.value.value is False
            ):
                offenders.append(f"{where} shell=<not False>")
            if isinstance(node, ast.Attribute) and node.attr in {"system", "popen", "Popen", "startfile", "getoutput", "getstatusoutput", "call", "check_call", "check_output"}:
                base = node.value
                if isinstance(base, ast.Name) and base.id in {"os", "subprocess"} and not (
                    base.id == "subprocess" and node.attr == "Popen" and path in _SHELL_FREE_ALLOWED_SUBPROCESS
                ):
                    offenders.append(f"{where} {base.id}.{node.attr}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.lower()
                # shell interpreters anywhere in a string
                for needle in ("cmd.exe", "cmd /c", "/bin/sh", "/bin/bash"):
                    if needle in text:
                        offenders.append(f"{where} string {needle!r}")
                # PowerShell script flags as whole argv tokens (a substring rule flagged ids like "unsupported-command");
                # only the module that reports PowerShell may carry them, for its informational version probe
                if text.strip() in ("-command", "-noprofile", "-encodedcommand") and path not in _MAY_NAME_POWERSHELL:
                    offenders.append(f"{where} string {node.value!r}")
                if "powershell" in text and path not in _MAY_NAME_POWERSHELL:
                    offenders.append(f"{where} string 'powershell'")
    assert offenders == []


def test_powershell_script_flags_are_used_only_by_the_informational_version_probes():
    from latex2word.toolchain import TOOL_SPECS, Role

    users = [s for s in TOOL_SPECS if any(str(a).lower() == "-command" for a in s.version_args)]
    assert {s.name for s in users} == {"powershell", "pwsh"}
    assert all(s.role is Role.INFORMATIONAL and s.windows_only for s in users)
    (arguments,) = {s.version_args for s in users}
    assert arguments[-1] == "$PSVersionTable.PSVersion.ToString()"  # a fixed expression, never user input


def test_subprocess_is_confined_to_the_two_audited_modules():
    users = sorted(
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if any(
            isinstance(n, (ast.Import, ast.ImportFrom))
            and ((isinstance(n, ast.Import) and any(a.name == "subprocess" for a in n.names))
                 or (isinstance(n, ast.ImportFrom) and n.module == "subprocess"))
            for n in ast.walk(_module_ast(p))
        )
    )
    assert users == ["process.py", "reference_doc.py"]


def test_production_code_has_no_executable_suffix_or_user_specific_paths():
    banned = (".exe", ".bat", ".cmd", "pathext", "c:\\users", "c:/users", "/home/", *PRIVATE_MARKERS)
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(_module_ast(path)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                offenders += [f"{path.name}: {b}" for b in banned if b in lowered and len(node.value) < 200]
    assert offenders == []


# --- process hygiene -----------------------------------------------------------------------------


def test_command_is_run_from_an_argv_array_so_metacharacters_stay_literal(tmp_path: Path):
    marker = tmp_path / "pwned"
    hostile = f"a & echo x > \"{marker}\" ; $(touch {marker}) | b"
    result = run_command(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", hostile], timeout=30, cwd=tmp_path
    )
    assert result.ok and result.stdout == hostile
    assert not marker.exists()


def test_text_input_reaches_the_child_without_newline_translation():
    code = "import sys; d = sys.stdin.buffer.read(); sys.stdout.write(repr(d))"
    result = run_command([sys.executable, "-c", code], timeout=30, input_text="a\nb\n\u00e9")
    assert result.stdout == repr(b"a\nb\n\xc3\xa9")


def test_crlf_output_is_reported_as_lf_and_invalid_utf8_is_replaced():
    code = "import sys; sys.stdout.buffer.write(b'one\\r\\ntwo\\xff\\r\\n')"
    result = run_command([sys.executable, "-c", code], timeout=30)
    assert result.stdout == "one\ntwo\ufffd\n"


def test_unlaunchable_program_is_reported_not_raised(tmp_path: Path):
    result = run_command([str(tmp_path / "no-such-tool")], timeout=5)
    assert result.error is not None and not result.ok


# --- configuration paths written the POSIX way and the Windows way ---------------------------------


def _config(tmp_path: Path, **project: object) -> Path:
    data = {"schema_version": 1, "project": {"name": "demo", "main": "main.tex", **project}, "profile": "ieee-conference"}
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_forward_slash_paths_are_the_portable_spelling(tmp_path: Path):
    config = load_config(_config(tmp_path, main="paper/main.tex"))
    assert config.project.main == (tmp_path / "paper" / "main.tex").resolve()


def test_posix_config_with_utf8_bom_and_crlf_loads(tmp_path: Path):
    path = tmp_path / "cfg.yaml"
    path.write_bytes(b"\xef\xbb\xbfschema_version: 1\r\nproject:\r\n  name: demo\r\n  main: paper/main.tex\r\nprofile: ieee-conference\r\n")
    assert load_config(path).project.main == (tmp_path / "paper" / "main.tex").resolve()


@pytest.mark.skipif(not WINDOWS, reason="backslash is a separator only for Windows pathlib")
@pytest.mark.parametrize("spelling", ["paper\\main.tex", "paper\\\\main.tex"])
def test_backslash_main_in_yaml_resolves_inside_the_project_on_windows(tmp_path: Path, spelling: str):
    text = f"schema_version: 1\nproject:\n  name: demo\n  main: '{spelling}'\nprofile: ieee-conference\n"
    (tmp_path / "cfg.yaml").write_text(text, encoding="utf-8")
    config = load_config(tmp_path / "cfg.yaml")
    assert config.project.main == (tmp_path / "paper" / "main.tex").resolve() or config.project.main.name != "main.tex"


@pytest.mark.skipif(not WINDOWS, reason="drive and UNC anchors exist only for Windows pathlib")
@pytest.mark.parametrize("main", ["C:\\x\\main.tex", "C:main.tex", "\\\\server\\share\\main.tex", "\\main.tex", "..\\main.tex"])
def test_windows_absolute_and_escaping_main_are_rejected_on_windows(tmp_path: Path, main: str):
    with pytest.raises(ConfigError) as info:
        load_config(_config(tmp_path, main=main))
    assert info.value.issues[0].path == "project.main"
