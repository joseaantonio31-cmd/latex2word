"""Builds the VISUAL_REVIEW_REQUIRED report of an integration run (runtime artifact, may hold absolute paths)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from latex2word.figure_pipeline import FiguresRun

REVIEW_LABEL = "VISUAL_REVIEW_REQUIRED"


def build_report(
    run: FiguresRun,
    project_root: Path,
    workspace: Path,
    baseline_svgs: dict[str, Path],
    comparisons: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for outcome in run.outcomes:
        record = outcome.record
        metrics = outcome.report.metrics if outcome.report else None
        preview = workspace / "previews" / f"{record.id}.png"
        rows.append(
            {
                "id": record.id,
                "review": REVIEW_LABEL,
                "original_source": str(project_root / record.source),
                "new_svg": str(workspace / record.output),
                "preview_png": str(preview) if preview.is_file() else None,
                "baseline_svg": str(baseline_svgs[record.id]) if record.id in baseline_svgs else None,
                "renderer": outcome.renderer,
                "dimensions": {
                    "width": metrics.width if metrics else None,
                    "height": metrics.height if metrics else None,
                    "viewBox": metrics.view_box if metrics else None,
                },
                "warnings": outcome.warnings,
                "comparison": comparisons.get(record.id),
                "pdf_routes": outcome.manifest_entry().get("pdf_routes"),
            }
        )
    return {"schema": "latex2word.integration_report/1", "figures": rows}


def _route_lines(routes: list[dict[str, Any]] | None) -> list[str]:
    if not routes:
        return []
    lines = ["- PDF to SVG routes:"]
    for route in routes:
        svg = route.get("svg") or {}
        lines.append(
            f"  - {route['backend']}: success={route['success']} viewBox={svg.get('viewBox')} "
            f"paths={svg.get('paths')} text={svg.get('text')} images={svg.get('images')} "
            f"fonts={svg.get('fonts')} size={svg.get('size_bytes')} warnings={route['warnings'] or 'none'}"
        )
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Figure review report", "", f"All figures: {REVIEW_LABEL} (no automatic visual-equivalence claim).", ""]
    for row in report["figures"]:
        lines += [
            f"## {row['id']}  [{row['review']}]",
            f"- original source: `{row['original_source']}`",
            f"- new SVG: `{row['new_svg']}`",
            f"- preview PNG: `{row['preview_png']}`",
            f"- baseline SVG: `{row['baseline_svg']}`",
            f"- renderer: {row['renderer']}",
            f"- dimensions: {row['dimensions']}",
            f"- warnings: {row['warnings'] or 'none'}",
            f"- baseline comparison: {json.dumps(row['comparison'])}",
            *_route_lines(row.get("pdf_routes")),
            "",
        ]
    return "\n".join(lines)
