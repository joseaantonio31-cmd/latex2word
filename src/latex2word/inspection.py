"""Read-only project inspection (``latex2word inspect``).

Never creates a workspace and never writes anywhere: it only builds the source
graph and the figure registry of the configured project.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from latex2word.config import ProjectConfig
from latex2word.diagnostics import Severity
from latex2word.errors import ExitCode
from latex2word.figure_registry import FigureRegistry, discover_figures
from latex2word.source_graph import Edge, Finding, SourceGraph, build_source_graph

INSPECT_SCHEMA = "latex2word.inspect/1"


def _edge_row(edge: Edge) -> dict[str, Any]:
    return {
        "reference": edge.raw,
        "kind": edge.kind.value,
        "resolved_to": edge.target,
        "location": edge.location.to_dict(),
    }


@dataclass(frozen=True)
class InspectionReport:
    project_name: str
    root: str
    main: str
    graph: SourceGraph
    figures: FigureRegistry

    @property
    def findings(self) -> list[Finding]:
        return [*self.graph.findings, *self.figures.findings]

    @property
    def has_errors(self) -> bool:
        return any(f.severity is Severity.ERROR for f in self.findings)

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.PROBLEMS if self.has_errors else ExitCode.OK

    def summary(self) -> dict[str, Any]:
        counts = {s: sum(1 for f in self.findings if f.severity is s) for s in Severity}
        figures_by_type: dict[str, int] = {}
        for figure in self.figures.active:
            figures_by_type[figure.source_type] = figures_by_type.get(figure.source_type, 0) + 1
        return {
            "status": "PROBLEMS" if self.has_errors else "OK",
            "tex_files": len(self.graph.tex_order),
            "figures_active": len(self.figures.active),
            "figures_inactive": len(self.figures.inactive),
            "figures_active_by_type": dict(sorted(figures_by_type.items())),
            "missing_dependencies": len(self.graph.missing),
            "outside_root_dependencies": len(self.graph.outside_root),
            "errors": counts[Severity.ERROR],
            "warnings": counts[Severity.WARNING],
            "infos": counts[Severity.INFO],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": INSPECT_SCHEMA,
            "project": {"name": self.project_name, "root": self.root, "main": self.main},
            "summary": self.summary(),
            "tex_files": list(self.graph.tex_order),
            "bibliography_files": self.graph.bibliography_files,
            "package_files": self.graph.package_files,
            "missing_dependencies": [_edge_row(e) | {"resolution": e.resolution.value} for e in self.graph.missing],
            "outside_root_dependencies": [
                _edge_row(e) | {"resolution": e.resolution.value} for e in self.graph.outside_root
            ],
            "figures": [f.to_dict() for f in self.figures.figures],
            "graph": self.graph.to_dict(),
            "findings": [f.to_dict() for f in self.findings],
        }


def inspect_project(config: ProjectConfig) -> InspectionReport:
    """Inspect ``config``'s project (read-only)."""
    graph = build_source_graph(config.project.root, config.project.main)
    figures = discover_figures(graph, config.project.root, config.project.main)
    main_rel = config.project.main.relative_to(config.project.root).as_posix()
    return InspectionReport(config.project.name, config.project.root.as_posix(), main_rel, graph, figures)


def render_json(report: InspectionReport) -> str:
    return json.dumps(report.to_dict(), indent=2)


def render_text(report: InspectionReport, *, verbose: bool = False) -> str:
    graph = report.graph
    summary = report.summary()
    lines = [
        f"Project:  {report.project_name}",
        f"Root:     {report.root}",
        f"Main:     {report.main}",
        "",
        f"TeX files ({len(graph.tex_order)}):",
        *[f"  {rel}" for rel in graph.tex_order],
    ]
    bibliography = graph.bibliography_files
    lines += ["", f"Bibliography files ({len(bibliography)}):", *[f"  {rel}" for rel in bibliography]]
    packages = graph.package_files
    lines += ["", f"Package files ({len(packages)}):", *[f"  {rel}" for rel in packages]]
    lines += ["", f"Missing dependencies ({len(graph.missing)}):"]
    lines += [f"  {e.location}: {e.kind.value} {e.raw!r}" for e in graph.missing]
    lines += ["", f"Dependencies outside the project root ({len(graph.outside_root)}):"]
    lines += [f"  {e.location}: {e.kind.value} {e.raw!r}" for e in graph.outside_root]
    lines += ["", f"Figures ({summary['figures_active']} active, {summary['figures_inactive']} inactive):"]
    for figure in report.figures.figures:
        state = "active" if figure.active else "inactive"
        width = figure.requested_width or "-"
        lines.append(
            f"  [{state:<8}] {figure.id}  type={figure.source_type}  source={figure.source}  "
            f"width={width}  renderer={figure.planned_renderer}  ({figure.source_file}:{figure.line})"
        )
    shown = [f for f in report.findings if verbose or f.severity is not Severity.INFO]
    lines += ["", f"Diagnostics ({len(shown)}):"]
    lines += [f"  {f}" for f in shown]
    lines += [
        "",
        f"Result: {summary['status']} ({summary['errors']} error(s), {summary['warnings']} warning(s))",
    ]
    return "\n".join(lines)
