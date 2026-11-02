from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import fake_which
from projectkit import (
    GOOD_SVG,
    PDF_BYTES,
    PNG_BYTES,
    ScriptedRunner,
    make_project,
    ok_result,
    tree_digest,
    write_project_config,
)

from latex2word.config import load_config
from latex2word.errors import ConfigError
from latex2word.figure_pipeline import run_figures
from latex2word.process import CommandResult
from latex2word.workspace import MARKER_NAME

FILES = {
    "main.tex": (
        "\\documentclass{article}\\usepackage[american]{circuitikz}\\usepackage{tikz}\n"
        "\\usetikzlibrary{arrows.meta}\n\\begin{document}\n"
        "\\input{figs/circuit}\\input{figs/chain}\\input{figs/plot}\\input{figs/photo}\n\\end{document}\n"
    ),
    "figs/circuit.tex": "\\begin{figure}\\resizebox{0.9\\columnwidth}{!}{\\begin{circuitikz}\\draw (0,0) to[R] (1,0);\\end{circuitikz}}\\label{fig:c}\\end{figure}",
    "figs/chain.tex": "\\begin{tikzpicture}\\node[tag, above] {recorded};\\end{tikzpicture}",
    "figs/plot.tex": "\\includegraphics[width=\\columnwidth]{figs/plot.pdf}",
    "figs/photo.tex": "\\includegraphics{figs/photo.png}",
    "figs/plot.pdf": PDF_BYTES,
    "figs/photo.png": PNG_BYTES,
    "figs/orphan.tex": "\\begin{circuitikz}\\end{circuitikz}",
    "scripts/make.py": "print('not a dependency')",
}
ALL = {"latex", "dvisvgm", "pdftocairo", "inkscape", "rsvg-convert"}


def tool_handler(*, fail: set[str] = frozenset()):
    def handler(argv: tuple[str, ...], cwd: Path | None) -> CommandResult:
        tool = Path(argv[0]).name
        if tool in fail:
            return CommandResult(argv, 1, "", f"{tool} failed", False, 0.01)
        if tool == "latex":
            (cwd / argv[-1]).with_suffix(".dvi").write_bytes(b"dvi")
        elif tool == "dvisvgm":
            Path(next(a for a in argv if a.startswith("--output=")).split("=", 1)[1]).write_text(GOOD_SVG, encoding="utf-8")
        elif tool == "pdftocairo":
            Path(argv[-1]).write_text(GOOD_SVG, encoding="utf-8")
        elif tool == "rsvg-convert":
            Path(argv[argv.index("-o") + 1]).write_bytes(b"png")
        return ok_result(argv)

    return handler


@pytest.fixture
def setup(tmp_path: Path):
    root = make_project(tmp_path, FILES)
    return tmp_path, root


def run(tmp_path: Path, root: Path, *, extra=None, fail=frozenset(), which=None, **kwargs):
    config = load_config(write_project_config(tmp_path, root, extra=extra))
    runner = ScriptedRunner(tool_handler(fail=fail))
    result = run_figures(config, which=which or fake_which(ALL), runner=runner, **kwargs)
    return config, result, runner


def test_end_to_end_renders_only_active_figures_and_writes_manifest(setup):
    tmp_path, root = setup
    before = tree_digest(root)
    config, result, runner = run(tmp_path, root)

    assert result.ok, result.problems
    assert [(o.record.id, o.renderer) for o in result.outcomes] == [
        ("circuit", "latex-dvisvgm"),
        ("chain", "latex-dvisvgm"),
        ("plot", "pdftocairo"),
        ("photo", "copy"),
    ]
    assert tree_digest(root) == before  # source tree untouched, no stray files
    workspace = config.workspace.root
    assert (workspace / MARKER_NAME).is_file()
    assert sorted(p.name for p in (workspace / "figures").iterdir()) == [
        "chain.svg", "circuit.svg", "figure_manifest.json", "photo.png", "plot.svg"
    ]
    staged = sorted(p.relative_to(workspace / "staging").as_posix() for p in (workspace / "staging").rglob("*") if p.is_file())
    assert staged == ["figs/chain.tex", "figs/circuit.tex", "figs/photo.png", "figs/photo.tex", "figs/plot.pdf", "figs/plot.tex", "main.tex"]
    assert runner.calls and all(cwd.is_relative_to(workspace) for _, cwd in runner.calls)  # tools ran in the workspace
    assert not any("orphan" in " ".join(argv) for argv, _ in runner.calls)


def test_manifest_has_relative_paths_hashes_and_metrics(setup):
    tmp_path, root = setup
    config, result, _ = run(tmp_path, root)
    text = result.manifest_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in text and str(root) not in text
    manifest = json.loads(text)
    assert manifest["schema_version"] == 1
    by_id = {f["id"]: f for f in manifest["figures"]}
    plot = by_id["plot"]
    assert plot["source"] == "figs/plot.pdf" and plot["renderer"] == "pdftocairo" and plot["status"] == "ok"
    assert plot["svg"]["viewBox"] == "0 0 10 5" and plot["svg"]["paths"] == 1
    import hashlib

    assert plot["output_sha256"] == hashlib.sha256((config.workspace.root / plot["output"]).read_bytes()).hexdigest()
    assert plot["source_sha256"] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert by_id["circuit"]["requested_width"] == "0.9\\columnwidth" and by_id["circuit"]["label"] == "fig:c"
    assert manifest["inactive_figures"] == [{"id": "orphan", "source_file": "figs/orphan.tex", "source_type": "circuitikz"}]
    diagnostics = json.loads(result.diagnostics_path.read_text(encoding="utf-8"))
    runs = diagnostics["figures"][0]["runs"]
    assert {"argv", "cwd", "stdout", "stderr", "returncode", "duration_s"} <= set(runs[0])


def test_render_document_is_derived_from_the_project_preamble(setup):
    tmp_path, root = setup
    config, _, _ = run(tmp_path, root)
    tex = (config.workspace.root / "render" / "circuit" / "circuit.tex").read_text(encoding="utf-8")
    assert "\\usepackage[american]{circuitikz}" in tex and "\\usetikzlibrary{arrows.meta}" in tex
    assert "\\def\\pgfsysdriver{pgfsys-dvisvgm.def}" in tex and "\\usepackage{lmodern}" in tex


def test_configured_patch_and_extra_preamble_apply_only_to_the_named_figure(setup):
    tmp_path, root = setup
    extra = {
        "figures": {
            "render_preamble": ["\\usepackage{xspace}"],
            "patches": [{"figure": "chain", "find": "node[tag, above]", "replace": "node[tag, above=6.5mm]"}],
        }
    }
    config, result, _ = run(tmp_path, root, extra=extra)
    assert result.ok
    work = config.workspace.root / "render"
    assert "above=6.5mm" in (work / "chain" / "chain.tex").read_text(encoding="utf-8")
    assert "above=6.5mm" not in (work / "circuit" / "circuit.tex").read_text(encoding="utf-8")
    assert "\\usepackage{xspace}" in (work / "circuit" / "circuit.tex").read_text(encoding="utf-8")
    # the project source itself still has the original text
    assert "above]" in (root / "figs" / "chain.tex").read_text(encoding="utf-8")


def test_unmatched_patch_fails_that_figure_only(setup):
    tmp_path, root = setup
    extra = {"figures": {"patches": [{"figure": "chain", "find": "no such text", "replace": "x"}]}}
    _, result, _ = run(tmp_path, root, extra=extra)
    assert not result.ok
    assert [o.record.id for o in result.failed] == ["chain"]
    assert "did not match" in result.failed[0].error
    assert sum(o.ok for o in result.outcomes) == 3


def test_failed_tool_fails_only_its_figure_and_is_reported(setup):
    tmp_path, root = setup
    _, result, _ = run(tmp_path, root, fail={"latex"})
    assert [o.record.id for o in result.failed] == ["circuit", "chain"]
    assert all("latex failed" in o.error for o in result.failed)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert [f["status"] for f in manifest["figures"]] == ["failed", "failed", "ok", "ok"]
    assert manifest["figures"][0]["output"] is None


def test_pdf_with_no_working_backend_fails_the_figure(setup):
    tmp_path, root = setup
    _, result, _ = run(tmp_path, root, fail={"pdftocairo", "inkscape", "dvisvgm"})
    plot = next(o for o in result.outcomes if o.record.id == "plot")
    assert not plot.ok and "no PDF to SVG backend" in plot.error


def test_previews_are_optional_and_never_fail_the_run(setup):
    tmp_path, root = setup
    _, without, _ = run(tmp_path, root, which=fake_which(ALL - {"rsvg-convert"}), previews=True)
    assert without.ok
    assert all(not o.preview.ok for o in without.outcomes if o.preview)
    assert any("rsvg-convert not found" in w for o in without.outcomes for w in o.warnings)
    config, with_tool, _ = run(tmp_path, root, previews=True)
    assert with_tool.ok
    assert sorted(p.name for p in (config.workspace.root / "previews").iterdir()) == ["chain.png", "circuit.png", "plot.png"]
    manifest = json.loads(with_tool.manifest_path.read_text(encoding="utf-8"))
    by_id = {f["id"]: f for f in manifest["figures"]}
    assert by_id["circuit"]["preview"] == "previews/circuit.png"
    assert "preview" not in by_id["photo"]  # raster passthrough is not previewed


def test_dependency_errors_stop_before_any_workspace_exists(tmp_path: Path):
    root = make_project(tmp_path, {"main.tex": "\\input{missing}\\includegraphics{gone}"})
    config, result, runner = run(tmp_path, root)
    assert not result.ok and result.workspace is None
    assert "dependency errors" in result.problems[0]
    assert not config.workspace.root.exists() and runner.calls == []


def test_extra_files_are_staged_and_missing_extras_are_problems(setup):
    tmp_path, root = setup
    config, result, _ = run(tmp_path, root, extra={"staging": {"extra_files": ["scripts/make.py"]}})
    assert result.ok and result.staging.by_source("scripts/make.py").dependency_type == "extra"
    assert (config.workspace.root / "staging" / "scripts" / "make.py").is_file()
    config2, bad, _ = run(tmp_path, root, extra={"staging": {"extra_files": ["nope.txt"]}})
    assert not bad.ok and "nope.txt" in bad.problems[0]


def test_workspace_inside_unowned_nonempty_directory_is_refused(setup):
    from latex2word.errors import WorkspaceSafetyError

    tmp_path, root = setup
    foreign = tmp_path / "workspace"
    foreign.mkdir()
    (foreign / "mine.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(WorkspaceSafetyError):
        run(tmp_path, root)
    assert (foreign / "mine.txt").read_text(encoding="utf-8") == "keep"


# ---- configuration extension ------------------------------------------------------------------------------------


def test_new_config_sections_load_and_default_config_to_dict_is_unchanged(setup):
    tmp_path, root = setup
    plain = load_config(write_project_config(tmp_path, root))
    assert "figures" not in plain.to_dict() and "staging" not in plain.to_dict()
    rich = load_config(
        write_project_config(
            tmp_path, root,
            extra={
                "figures": {"pdf_backends": ["inkscape"], "render_preamble": ["\\x"], "patches": [{"figure": "a", "find": "b", "replace": ""}]},
                "staging": {"extra_files": ["scripts/make.py"]},
            },
        )
    )
    assert rich.figures.pdf_backends == ("inkscape",) and rich.staging.extra_files == ("scripts/make.py",)
    assert rich.to_dict()["figures"]["patches"] == [{"figure": "a", "find": "b", "replace": ""}]


@pytest.mark.parametrize(
    "extra",
    [
        {"figures": {"pdf_backends": ["magic"]}},
        {"figures": {"pdf_backends": []}},
        {"figures": {"patches": [{"figure": "a", "find": ""}]}},
        {"staging": {"extra_files": ["../escape.txt"]}},
        {"staging": {"extra_files": ["/abs.txt"]}},
        {"staging": {"unknown": 1}},
    ],
)
def test_invalid_new_config_sections_are_rejected(setup, extra):
    tmp_path, root = setup
    with pytest.raises(ConfigError):
        load_config(write_project_config(tmp_path, root, extra=extra))
