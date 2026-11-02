from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import fake_which
from projectkit import GOOD_SVG, PDF_BYTES, PNG_BYTES, ScriptedRunner, make_project, tree_digest, write_project_config
from test_figure_pipeline import ALL, tool_handler

from latex2word.config import load_config
from latex2word.errors import BuildError
from latex2word.figure_pipeline import run_figures
from latex2word.figure_rewrite import copy_assets, parse_width, plan_figure_rewrites, remaining_tikz
from latex2word.rewrite import Edit, apply_edits, drop_contained, manifest
from latex2word.staging import staging_dir

FILES = {
    "main.tex": (
        "\\documentclass{article}\\usepackage{tikz}\\begin{document}\n"
        "\\begin{figure}\\resizebox{0.9\\columnwidth}{!}{%\n\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}%\n}\n"
        "\\caption{Circuit}\\label{fig:c}\\end{figure}\n"
        "\\begin{figure}\\begin{tikzpicture}\\node{x};\\end{tikzpicture}\\end{figure}\n"
        "\\includegraphics[width=\\columnwidth]{figs/plot.pdf}\n"
        "\\includegraphics[width=.5\\textwidth, keepaspectratio]{figs/photo.png}\n"
        "\\includegraphics[width=\\mywidth]{figs/photo.png}\n"
        "\\includegraphics[scale=0.5]{figs/photo.png}\n"
        "\\end{document}\n"
    ),
    "figs/plot.pdf": PDF_BYTES,
    "figs/photo.png": PNG_BYTES,
}


def prepare(tmp_path: Path, files=None, main="main.tex"):
    root = make_project(tmp_path, files or FILES)
    config = load_config(write_project_config(tmp_path, root, main=main))
    run = run_figures(config, which=fake_which(ALL), runner=ScriptedRunner(tool_handler()))
    assert run.ok, run.problems
    return root, config, run


@pytest.mark.parametrize(
    ("expression", "kind", "emitted"),
    [
        ("0.98\\columnwidth", "relative", "0.98\\linewidth"),
        (".5\\textwidth", "relative", "0.5\\linewidth"),
        ("\\linewidth", "relative", "1\\linewidth"),
        ("  0.25  \\linewidth ", "relative", "0.25\\linewidth"),
        ("3.5in", "absolute", "3.5in"),
        ("8 cm", "absolute", "8cm"),
        ("100pt", "absolute", "100pt"),
        ("72bp", "absolute", "1in"),
        ("\\mywidth", "unsupported", None),
        ("0.5\\dimexpr\\linewidth", "unsupported", None),
        ("0\\linewidth", "unsupported", None),
        ("0.5\\textheight", "unsupported", None),
        ("", "unsupported", None),
    ],
)
def test_width_expressions_become_an_intermediate_representation(expression: str, kind: str, emitted: str | None):
    ir = parse_width(expression)
    assert (ir.kind, ir.emitted) == (kind, emitted)
    assert ir.original == " ".join(expression.split())


def test_relative_and_absolute_widths_carry_their_numbers():
    relative = parse_width("0.98\\columnwidth")
    assert (relative.fraction, relative.basis, relative.inches) == (0.98, "columnwidth", None)
    assert parse_width("2.54cm").inches == pytest.approx(1.0)
    assert parse_width("36pt").inches == pytest.approx(0.5)


def test_tikz_pdf_and_widths_are_rewritten_in_the_staged_copy_only(tmp_path: Path):
    root, _, run = prepare(tmp_path)
    before = tree_digest(root)
    plan = plan_figure_rewrites(run, "main.tex")
    texts = {e.kind: e for e in plan.edits}
    tikz = [e for e in plan.edits if e.kind == "figure-tikz"]
    assert [e.replacement for e in tikz] == [
        "\\includegraphics[width=0.9\\linewidth]{_l2w/figures/main.svg}",
        "\\includegraphics{_l2w/figures/main-2.svg}",
    ]
    assert texts["figure-asset"].replacement == "\\includegraphics[width=1\\linewidth]{_l2w/figures/plot.svg}"
    widths = [e for e in plan.edits if e.kind == "figure-width"]
    assert [e.replacement for e in widths] == ["\\includegraphics[width=0.5\\linewidth,keepaspectratio]{figs/photo.png}"]
    assert plan.assets == [
        ("figures/main.svg", "_l2w/figures/main.svg"), ("figures/main-2.svg", "_l2w/figures/main-2.svg"),
        ("figures/plot.svg", "_l2w/figures/plot.svg"),
    ]
    assert tree_digest(root) == before


def test_applying_edits_writes_the_staged_files_and_records_every_transformation(tmp_path: Path):
    root, config, run = prepare(tmp_path)
    plan = plan_figure_rewrites(run, "main.tex")
    workspace = config.workspace.root
    staging = staging_dir(type("W", (), {"path": workspace})())
    copy_assets(plan, workspace, staging)
    records, changes = apply_edits(run.inspection.graph, staging, plan.edits)
    staged = (staging / "main.tex").read_text(encoding="utf-8")
    assert "tikzpicture" not in staged and "\\resizebox" not in staged
    assert "\\includegraphics[width=0.9\\linewidth]{_l2w/figures/main.svg}" in staged
    assert (staging / "_l2w" / "figures" / "plot.svg").read_text(encoding="utf-8") == GOOD_SVG
    assert (root / "main.tex").read_text(encoding="utf-8").count("tikzpicture") == 4  # the source is untouched
    original = [r for r in records if r.kind == "figure-tikz"][0]
    assert original.original.startswith("\\resizebox{0.9\\columnwidth}{!}{%") and original.source == "main.tex"
    assert original.line == 2 and "Pandoc cannot render TikZ" in original.reason
    data = manifest(records, changes)
    assert data["files"][0]["sha256_before"] != data["files"][0]["sha256_after"]
    assert {t["kind"] for t in data["transformations"]} == {"figure-tikz", "figure-asset", "figure-width"}
    assert remaining_tikz({"main.tex": staged}) == []
    assert len(remaining_tikz({"main.tex": (root / "main.tex").read_text(encoding="utf-8")})) == 2


def test_unsupported_width_and_ignored_options_are_reported_never_guessed(tmp_path: Path):
    _, _, run = prepare(tmp_path)
    plan = plan_figure_rewrites(run, "main.tex")
    by_code = {f.code: f for f in plan.findings}
    assert "\\mywidth" in by_code["L2W-W-FIG-003"].message and "intrinsic size" in by_code["L2W-W-FIG-003"].message
    assert "scale=0.5" in by_code["L2W-W-FIG-004"].message
    text = run.inspection.graph.files["main.tex"].text
    assert not any("mywidth" in text[edit.start : edit.end] for edit in plan.edits)  # left exactly as written


def test_figure_info_keeps_the_requested_width_and_aspect_for_validation(tmp_path: Path):
    _, _, run = prepare(tmp_path)
    plan = plan_figure_rewrites(run, "main.tex")
    info = {f.id: f for f in plan.figures}
    assert info["main"].width.fraction == 0.9 and info["main"].width.original == "0.9\\columnwidth"
    assert info["main-2"].width is None
    assert info["plot"].asset == "figures/plot.svg" and info["plot"].aspect == pytest.approx(2.0, rel=1e-3)
    assert info["plot"].asset_sha256 and info["plot"].staged_asset == "_l2w/figures/plot.svg"
    assert json.dumps([f.to_dict() for f in plan.figures])


def test_height_in_resizebox_is_flagged(tmp_path: Path):
    files = {
        "main.tex": "\\begin{document}\\resizebox{3in}{2in}{\\begin{tikzpicture}\\draw (0,0);\\end{tikzpicture}}\\end{document}",
    }
    _, _, run = prepare(tmp_path, files)
    plan = plan_figure_rewrites(run, "main.tex")
    assert [f.code for f in plan.findings] == ["L2W-W-FIG-006"]
    assert plan.edits[0].replacement == "\\includegraphics[width=3in]{_l2w/figures/main.svg}"


def test_assets_are_staged_next_to_a_main_file_in_a_subdirectory(tmp_path: Path):
    files = {"paper/main.tex": "\\begin{document}\\includegraphics[width=2in]{figs/p.pdf}\\end{document}", "paper/figs/p.pdf": PDF_BYTES}
    _, _, run = prepare(tmp_path, files, main="paper/main.tex")
    plan = plan_figure_rewrites(run, "paper/main.tex")
    assert plan.assets == [("figures/p.svg", "paper/_l2w/figures/p.svg")]
    assert plan.edits[0].replacement == "\\includegraphics[width=2in]{_l2w/figures/p.svg}"


def test_contained_edits_are_dropped_and_partial_overlaps_are_errors(tmp_path: Path):
    outer = Edit("main.tex", 10, 50, "X", "k", "r")
    inner = Edit("main.tex", 20, 30, "Y", "k", "r")
    assert drop_contained([inner, outer]) == [outer]
    _, _, run = prepare(tmp_path)
    staging = staging_dir(type("W", (), {"path": run.workspace})())
    with pytest.raises(BuildError, match="overlapping"):
        apply_edits(run.inspection.graph, staging, [Edit("main.tex", 5, 20, "", "a", "r"), Edit("main.tex", 10, 30, "", "b", "r")])


def test_resizebox_around_the_input_of_a_tikz_file_is_unwrapped_and_keeps_its_width(tmp_path: Path):
    """``\\resizebox{W}{!}{\\input{fig}}`` where fig.tex holds only the drawing (comments around it are fine)."""
    files = {
        "main.tex": (
            "\\documentclass{report}\\usepackage{tikz}\\begin{document}\n"
            "\\begin{figure}\\centering\n\\resizebox{\\textwidth}{!}{\\input{Figuras/circ}}\n"
            "\\caption{C}\\label{fig:circ}\\end{figure}\n\\end{document}\n"
        ),
        "Figuras/circ.tex": "% a circuit\n\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}\n% end\n",
    }
    root, config, run = prepare(tmp_path, files)
    plan = plan_figure_rewrites(run, "main.tex")
    workspace = config.workspace.root
    staging = staging_dir(type("W", (), {"path": workspace})())
    copy_assets(plan, workspace, staging)
    apply_edits(run.inspection.graph, staging, plan.edits)
    main = (staging / "main.tex").read_text(encoding="utf-8")
    drawing = (staging / "Figuras" / "circ.tex").read_text(encoding="utf-8")
    assert "\\resizebox" not in main and "\\input{Figuras/circ}" in main
    assert "\\includegraphics[width=1\\linewidth]{_l2w/figures/circ.svg}" in drawing
    assert [f.width.original for f in plan.figures if f.id == "circ"] == ["\\textwidth"]

