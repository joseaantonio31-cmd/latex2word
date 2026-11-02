"""The ``figures`` pipeline: inspect, stage, render active figures, validate, write the manifest.

No Pandoc, no DOCX: this stage only produces figure assets and evidence inside
the latex2word-owned workspace.

Workspace layout::

    <ws>/staging/...                 reachable sources, project-relative paths
    <ws>/staging_manifest.json
    <ws>/render/<figure id>/...      per-figure render directories (tex, dvi, logs, backend SVGs)
    <ws>/figures/<figure id>.<ext>   produced assets
    <ws>/figures/figure_manifest.json
    <ws>/previews/<figure id>.png    only with previews enabled
    <ws>/diagnostics/figures_run.json  tool runs (argv, cwd, stdout, stderr, exit code, time)
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from latex2word.config import ProjectConfig
from latex2word.diagnostics import Severity
from latex2word.figure_registry import FigureRecord, sha256_file
from latex2word.inspection import InspectionReport, inspect_project
from latex2word.logging_utils import log_event
from latex2word.pdf_svg import DEFAULT_PDF_BACKENDS, BackendAttempt, convert_pdf_to_svg
from latex2word.previews import PreviewResult, render_preview
from latex2word.process import run_command
from latex2word.staging import StagingManifest, stage_files, staging_dir, write_staging_manifest
from latex2word.svg_validate import SvgReport, validate_svg_file
from latex2word.tikz import build_render_document, derive_render_preamble
from latex2word.tikz_render import render_tikz
from latex2word.toolrun import Runner, ToolRun, Which
from latex2word.workspace import Workspace, is_link_like, verify_owned

_log = logging.getLogger(__name__)

FIGURES_SCHEMA = "latex2word.figures/1"
MANIFEST_RELATIVE = "figures/figure_manifest.json"
DIAGNOSTICS_RELATIVE = "diagnostics/figures_run.json"
_WORK_DIRS = ("render", "figures", "previews", "diagnostics")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


@dataclass
class FigureOutcome:
    record: FigureRecord
    ok: bool = False
    renderer: str | None = None
    error: str | None = None
    output_sha256: str | None = None
    report: SvgReport | None = None
    warnings: list[str] = field(default_factory=list)
    runs: list[ToolRun] = field(default_factory=list)
    pdf_attempts: list[BackendAttempt] = field(default_factory=list)
    preview: PreviewResult | None = None

    def manifest_entry(self) -> dict[str, Any]:
        record = self.record
        metrics = self.report.metrics if self.report else None
        svg: dict[str, Any] | None = None
        if metrics is not None:
            svg = {
                "viewBox": metrics.view_box,
                "width": metrics.width,
                "height": metrics.height,
                "paths": metrics.paths,
                "text": metrics.text,
                "images": metrics.images,
                "fonts": list(metrics.fonts),
                "size_bytes": metrics.size_bytes,
            }
        entry: dict[str, Any] = {
            "id": record.id,
            "label": record.label,
            "source": record.source,
            "source_file": record.source_file,
            "line": record.line,
            "environment": record.environment,
            "source_type": record.source_type,
            "requested_width": record.requested_width,
            "status": "ok" if self.ok else "failed",
            "renderer": self.renderer,
            "source_sha256": record.source_sha256,
            "output": record.output if self.ok else None,
            "output_sha256": self.output_sha256,
            "svg": svg,
            "warnings": list(self.warnings),
            "error": self.error,
        }
        if self.pdf_attempts and len(self.pdf_attempts) > 1:
            entry["pdf_routes"] = [_route_summary(a) for a in self.pdf_attempts]
        if self.preview is not None:
            entry["preview"] = f"previews/{record.id}.png" if self.preview.ok else None
            entry["preview_status"] = self.preview.message
        return entry


def _route_summary(attempt: BackendAttempt) -> dict[str, Any]:
    metrics = attempt.report.metrics if attempt.report else None
    return {
        "backend": attempt.backend,
        "available": attempt.available,
        "success": attempt.success,
        "error": attempt.error,
        "warnings": attempt.warnings(),
        "svg": metrics.to_dict() if metrics else None,
        "issues": [str(i) for i in attempt.report.issues] if attempt.report else [],
    }


@dataclass
class FiguresRun:
    inspection: InspectionReport
    problems: list[str] = field(default_factory=list)
    workspace: Path | None = None
    staging: StagingManifest | None = None
    outcomes: list[FigureOutcome] = field(default_factory=list)
    manifest: dict[str, Any] | None = None
    manifest_path: Path | None = None
    diagnostics_path: Path | None = None
    previews_requested: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems and all(o.ok for o in self.outcomes)

    @property
    def failed(self) -> list[FigureOutcome]:
        return [o for o in self.outcomes if not o.ok]

    def summary(self) -> dict[str, Any]:
        return {
            "status": "OK" if self.ok else "PROBLEMS",
            "active_figures": len(self.outcomes),
            "rendered": sum(1 for o in self.outcomes if o.ok),
            "failed": len(self.failed),
            "staged_files": len(self.staging.files) if self.staging else 0,
            "problems": list(self.problems),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "latex2word.figures_run/1",
            "summary": self.summary(),
            "workspace": self.workspace.as_posix() if self.workspace else None,
            "manifest": MANIFEST_RELATIVE if self.manifest_path else None,
            "figures": [o.manifest_entry() for o in self.outcomes],
            "inactive_figures": [{"id": f.id, "source_file": f.source_file} for f in self.inspection.figures.inactive],
            "findings": [f.to_dict() for f in self.inspection.findings if f.severity is not Severity.INFO],
        }


def _reset_work_dirs(workspace: Workspace) -> None:
    verify_owned(workspace.path)
    for name in _WORK_DIRS:
        target = workspace.path / name
        if is_link_like(target):
            raise OSError(f"refusing link in workspace: {target}")
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()


def _apply_patches(figure_id: str, drawing: str, config: ProjectConfig) -> tuple[str, str | None]:
    for patch in config.figures.patches:
        if patch.figure != figure_id:
            continue
        if patch.find not in drawing:
            return drawing, f"configured patch for {figure_id!r} did not match: {patch.find!r}"
        drawing = drawing.replace(patch.find, patch.replace)
    return drawing, None


def _render_one(
    record: FigureRecord,
    run: "FiguresRun",
    config: ProjectConfig,
    workspace: Workspace,
    *,
    compare_pdf: bool,
    which: Which,
    runner: Runner,
) -> FigureOutcome:
    outcome = FigureOutcome(record)
    graph = run.inspection.graph
    timeout = config.tools.timeout_s
    output = workspace.path / record.output
    workdir = workspace.path / "render" / record.id
    staged = staging_dir(workspace)

    if record.is_tikz:
        scanned = graph.files[record.source_file]
        assert record.span is not None
        drawing = scanned.text[record.span[0] : record.span[1]]
        drawing, patch_error = _apply_patches(record.id, drawing, config)
        if patch_error:
            outcome.error = patch_error
            return outcome
        main_rel = run.inspection.main
        preamble = derive_render_preamble(graph.files[main_rel].text, drawing)
        document = build_render_document(drawing, preamble, record.source_type, config.figures.render_preamble)
        source_dir = (staged / main_rel).parent
        result = render_tikz(
            record.id, document, workdir, output, timeout=timeout, which=which, runner=runner, source_dir=source_dir
        )
        outcome.runs = result.runs
        outcome.report = result.report
        outcome.renderer = record.planned_renderer
        outcome.error = result.error
        outcome.ok = result.ok
    else:
        assert record.input_asset is not None
        source = staged / Path(*record.input_asset.split("/"))
        if record.source_type == "pdf":
            backends = config.figures.pdf_backends or DEFAULT_PDF_BACKENDS
            conversion = convert_pdf_to_svg(
                source, output, workdir, backends=backends, timeout=timeout, which=which, runner=runner,
                compare_all=compare_pdf,
            )
            outcome.pdf_attempts = conversion.attempts
            outcome.runs = [a.run for a in conversion.attempts if a.run is not None]
            if conversion.selected is not None:
                outcome.ok = True
                outcome.renderer = conversion.selected.backend
                outcome.report = conversion.selected.report
                outcome.warnings = conversion.selected.warnings()
            else:
                reasons = "; ".join(f"{a.backend}: {a.error}" for a in conversion.attempts)
                outcome.error = f"no PDF to SVG backend produced a valid SVG ({reasons})"
                outcome.warnings = [w for a in conversion.attempts for w in a.warnings()]
        elif record.source_type == "svg":
            outcome.renderer = record.planned_renderer
            outcome.report = validate_svg_file(source, expect_vector=False)
            if outcome.report.valid:
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, output)
                outcome.ok = True
            else:
                outcome.error = "invalid SVG: " + "; ".join(i.message for i in outcome.report.errors)
        else:
            magic = _PNG_MAGIC if record.source_type == "png" else _JPEG_MAGIC
            outcome.renderer = record.planned_renderer
            with source.open("rb") as handle:
                header = handle.read(len(magic))
            if header != magic:
                outcome.error = f"file does not look like a {record.source_type.upper()} image"
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, output)
                outcome.ok = True
    if outcome.report is not None and not outcome.warnings:
        outcome.warnings = [str(i) for i in outcome.report.warnings]
    if outcome.ok and output.is_file():
        outcome.output_sha256 = sha256_file(output)
    return outcome


def run_figures(
    config: ProjectConfig,
    *,
    previews: bool = False,
    compare_pdf_backends: bool = False,
    which: Which = shutil.which,
    runner: Runner = run_command,
) -> FiguresRun:
    """Run the figure pipeline for ``config`` (see the module docstring)."""
    inspection = inspect_project(config)
    run = FiguresRun(inspection, previews_requested=previews)
    if inspection.has_errors:
        run.problems.append("project has dependency errors; run `latex2word inspect` for details")
        return run
    root = config.project.root
    extras = list(dict.fromkeys([*config.staging.extra_files, *config.bibliography.files]))
    for extra in extras:
        if not (root / extra).is_file():
            run.problems.append(f"{extra} (staging.extra_files or bibliography.files) does not exist")
    if run.problems:
        return run

    workspace = Workspace.create(config.workspace.root, root)
    run.workspace = workspace.path
    entries = [*inspection.graph.stageable_files(), *((e, "extra") for e in extras)]
    run.staging = stage_files(root, entries, workspace)
    write_staging_manifest(run.staging, workspace)
    _reset_work_dirs(workspace)

    for record in inspection.figures.active:
        outcome = _render_one(
            record, run, config, workspace, compare_pdf=compare_pdf_backends, which=which, runner=runner
        )
        output = workspace.path / record.output
        if previews and outcome.ok and record.output.endswith(".svg"):
            outcome.preview = render_preview(
                output,
                workspace.path / "previews" / f"{record.id}.png",
                workspace.path / "render" / record.id,
                timeout=config.tools.timeout_s,
                which=which,
                runner=runner,
            )
            outcome.runs.extend([outcome.preview.run] if outcome.preview.run else [])
            if not outcome.preview.ok:
                outcome.warnings.append(outcome.preview.message)
        run.outcomes.append(outcome)
        log_event(_log, logging.INFO, "figure processed", id=record.id, ok=outcome.ok, renderer=outcome.renderer)

    run.manifest = build_manifest(run)
    run.manifest_path = workspace.path / MANIFEST_RELATIVE
    run.manifest_path.write_bytes((json.dumps(run.manifest, indent=2) + "\n").encode("utf-8"))
    run.diagnostics_path = workspace.path / DIAGNOSTICS_RELATIVE
    run.diagnostics_path.write_bytes((json.dumps(build_diagnostics(run), indent=2) + "\n").encode("utf-8"))
    return run


def build_manifest(run: FiguresRun) -> dict[str, Any]:
    """The figure manifest: project-relative sources, workspace-relative outputs, no absolute paths."""
    return {
        "schema": FIGURES_SCHEMA,
        "schema_version": 1,
        "project": {"name": run.inspection.project_name, "main": run.inspection.main},
        "figures": [o.manifest_entry() for o in run.outcomes],
        "inactive_figures": [
            {"id": f.id, "source_file": f.source_file, "source_type": f.source_type}
            for f in run.inspection.figures.inactive
        ],
    }


def build_diagnostics(run: FiguresRun) -> dict[str, Any]:
    """Runtime diagnostic artifact (contains absolute paths: argv and cwd of every tool run)."""
    return {
        "schema": "latex2word.figures_diagnostics/1",
        "figures": [
            {
                "id": o.record.id,
                "error": o.error,
                "runs": [r.to_dict() for r in o.runs],
                "pdf_routes": [a.to_dict() for a in o.pdf_attempts],
                "preview": o.preview.to_dict() if o.preview else None,
            }
            for o in run.outcomes
        ],
    }


def render_text(run: FiguresRun) -> str:
    lines = [f"Project: {run.inspection.project_name}"]
    if run.workspace:
        lines.append(f"Workspace: {run.workspace}")
    for problem in run.problems:
        lines.append(f"problem: {problem}")
    if run.staging:
        lines.append(f"Staged files: {len(run.staging.files)}")
    lines.append("")
    lines.append(f"Figures ({len(run.outcomes)} active):")
    for outcome in run.outcomes:
        record = outcome.record
        status = "ok" if outcome.ok else "FAILED"
        detail = ""
        if outcome.report and outcome.report.metrics:
            m = outcome.report.metrics
            detail = f"  viewBox={m.view_box}  paths={m.paths} text={m.text} images={m.images}"
        lines.append(f"  [{status}] {record.id}  type={record.source_type}  renderer={outcome.renderer}{detail}")
        if outcome.error:
            lines.append(f"      error: {outcome.error}")
        for warning in outcome.warnings:
            lines.append(f"      warning: {warning}")
    if run.inspection.figures.inactive:
        lines.append("")
        lines.append(
            "Inactive (not rendered): " + ", ".join(f.id for f in run.inspection.figures.inactive)
        )
    if run.manifest_path:
        lines += ["", f"Manifest: {run.manifest_path}"]
    summary = run.summary()
    lines += ["", f"Result: {summary['status']} ({summary['rendered']}/{summary['active_figures']} rendered)"]
    return "\n".join(lines)
