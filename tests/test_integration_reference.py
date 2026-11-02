"""External reference-project integration of the figure pipeline (deselected by default; run with ``-m integration``).

Environment (all read-only inputs):

* ``LATEX2WORD_INTEGRATION_PAPER_ROOT``    - root of the reference LaTeX project;
* ``LATEX2WORD_INTEGRATION_EXPECTATIONS``  - local JSON file with the project's expectations (``integration_project``);
* ``LATEX2WORD_BASELINE_ROOT``             - an earlier reference conversion of the project (comparison evidence);
* ``LATEX2WORD_INTEGRATION_MAIN``          - optional main file (default ``main.tex``);
* ``LATEX2WORD_INTEGRATION_WORKSPACE``     - optional workspace directory (default: a tmp dir).

Without them the tests skip. Project-specific figure patches come from the expectations, never from production code.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from integration_project import expect
from integration_report import build_report, render_markdown
from projectkit import tree_digest

from latex2word.config import load_config
from latex2word.figure_pipeline import run_figures
from latex2word.inspection import inspect_project
from latex2word.svg_validate import validate_svg_file

pytestmark = pytest.mark.integration


def _env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value or not Path(value).is_dir():
        pytest.skip(f"{name} is not set to an existing directory")
    return Path(value)


@pytest.fixture(scope="module")
def paper_root() -> Path:
    return _env_path("LATEX2WORD_INTEGRATION_PAPER_ROOT")


@pytest.fixture(scope="module")
def baseline_svgs() -> tuple[dict[str, Path], dict[str, str]]:
    root = _env_path("LATEX2WORD_BASELINE_ROOT")
    found = [p for p in root.rglob("figures_word") if p.is_dir() and "pristine" in p.as_posix()]
    if not found:
        pytest.skip("no pristine figures_word directory in the baseline")
    svgs = {p.stem: p for p in found[0].glob("*.svg")}
    hashes: dict[str, str] = {}
    manifest = root / "baseline_manifest.json"
    if manifest.is_file():
        assets = json.loads(manifest.read_text(encoding="utf-8")).get("generated_assets", {}).get("word_build_files", {})
        hashes = {Path(k).stem: v["sha256"] for k, v in assets.items() if k.endswith(".svg")}
    return svgs, hashes


@pytest.fixture(scope="module")
def configured(paper_root: Path, tmp_path_factory: pytest.TempPathFactory):
    base = tmp_path_factory.mktemp("integration")
    workspace = Path(os.environ.get("LATEX2WORD_INTEGRATION_WORKSPACE") or base / "workspace")
    main = os.environ.get("LATEX2WORD_INTEGRATION_MAIN", "main.tex")
    config_path = base / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "project": {"name": "integration-reference", "root": str(paper_root), "main": main},
                "profile": "ieee-conference",
                "workspace": {"root": str(workspace)},
                "figures": {"patches": expect("figure_patches")},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return load_config(config_path)


@pytest.fixture(scope="module")
def tools_ready() -> None:
    missing = [t for t in ("latex", "dvisvgm", "pdftocairo") if shutil.which(t) is None]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")


@pytest.fixture(scope="module")
def outcome(configured, paper_root: Path, tools_ready):
    before = tree_digest(paper_root)
    run = run_figures(configured, previews=True, compare_pdf_backends=True)
    after = tree_digest(paper_root)
    return run, before, after


def test_source_graph_and_figure_facts(configured):
    report = inspect_project(configured)
    assert not report.has_errors, [str(f) for f in report.findings]
    assert report.graph.bibliography_files
    active = {f.id: f.source_type for f in report.figures.active}
    assert active == expect("figures", "active")
    expected_tikz = sorted(kind for kind in active.values() if kind in ("tikzpicture", "circuitikz"))
    assert sorted(f.source_type for f in report.figures.active if f.is_tikz) == expected_tikz
    assert [f.id for f in report.figures.inactive] == expect("figures", "inactive")


def test_source_project_is_byte_identical_after_the_run(outcome):
    _, before, after = outcome
    assert before == after


def test_staging_contains_only_reachable_files(outcome):
    run, _, _ = outcome
    staged = {f.source for f in run.staging.files}
    assert not staged & set(expect("figures", "staged_absent"))
    assert not any(s.startswith(tuple(expect("figures", "staged_absent_prefixes"))) for s in staged)
    assert set(expect("figures", "staged_present")) <= staged


def test_figures_render_and_match_baseline_structure(outcome, baseline_svgs, configured):
    run, _, _ = outcome
    svgs, hashes = baseline_svgs
    assert run.ok, [(o.record.id, o.error) for o in run.failed]
    assert {o.record.id for o in run.outcomes} == set(expect("figures", "active"))
    comparisons: dict[str, dict[str, Any]] = {}
    for o in run.outcomes:
        new = validate_svg_file(configured.workspace.root / o.record.output)
        assert new.valid and new.metrics.images == 0 and new.metrics.text == 0
        base = validate_svg_file(svgs[o.record.id])
        nm, bm = new.metrics, base.metrics
        aspect_new, aspect_base = nm.width_px / nm.height_px, bm.width_px / bm.height_px
        assert aspect_new == pytest.approx(aspect_base, rel=0.01)
        comparisons[o.record.id] = {
            "renderer": o.renderer,
            "baseline_view_box": bm.view_box,
            "view_box": nm.view_box,
            "view_box_equal": nm.view_box == bm.view_box,
            "paths": [nm.paths, bm.paths],
            "hash_identical": o.output_sha256 == hashes.get(o.record.id),
        }
        if o.record.is_tikz:
            assert o.renderer == "latex-dvisvgm"
            assert nm.view_box == bm.view_box and nm.paths == bm.paths
        else:
            assert o.renderer == "pdftocairo"
    workspace = configured.workspace.root
    report = build_report(run, configured.project.root, workspace, svgs, comparisons)
    assert all(r["review"] == "VISUAL_REVIEW_REQUIRED" for r in report["figures"])
    out_dir = workspace / "report"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "figure_review.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "figure_review.md").write_text(render_markdown(report), encoding="utf-8")


def test_inactive_figure_is_not_rendered_and_previews_exist(outcome, configured):
    run, _, _ = outcome
    workspace = configured.workspace.root
    for inactive in expect("figures", "inactive"):
        assert not list((workspace / "figures").glob(f"{inactive}*"))
    for o in run.outcomes:
        assert o.preview is None or o.preview.ok or "rsvg-convert" in o.preview.message
