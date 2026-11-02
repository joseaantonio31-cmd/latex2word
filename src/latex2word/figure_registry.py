"""Figure discovery: which figures does the reachable source graph actually use?

Active figures come from the reachable TeX files only. Figure wrappers that no
reachable file includes (orphans) are reported as *inactive* and are never
rendered by default. Orphans are looked for only next to reachable files, never
by crawling the whole project tree.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from latex2word.diagnostics import Severity
from latex2word.latex_scan import (
    Environment,
    iter_commands,
    read_group,
    scan_environments,
    skip_ws,
)
from latex2word.logging_utils import log_event
from latex2word.source_graph import (
    EdgeKind,
    Finding,
    Resolution,
    ScannedFile,
    SourceGraph,
    SourceLocation,
    scan_isolated_file,
)
from latex2word.workspace import is_link_like

_log = logging.getLogger(__name__)

FLOAT_ENVIRONMENTS = ("figure", "figure*")
TIKZ_SOURCE_TYPES = ("tikzpicture", "circuitikz")
RENDERER_TIKZ = "latex-dvisvgm"
RENDERER_PDF = "pdf-to-svg"
RENDERER_COPY = "copy"

_LABEL_SAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class FigureRecord:
    id: str
    source_file: str  # TeX file containing the figure (project-relative POSIX)
    line: int
    environment: str | None  # enclosing float environment, if any
    label: str | None
    input_asset: str | None  # graphics asset (project-relative); None for TikZ/circuitikz
    source_type: str  # tikzpicture | circuitikz | pdf | svg | png | jpeg
    requested_width: str | None
    active: bool
    planned_renderer: str
    output: str  # workspace-relative POSIX path of the produced asset
    source_sha256: str | None  # asset bytes (graphics) or extracted environment text (TikZ)
    span: tuple[int, int] | None = None  # (begin, end) offsets of a TikZ environment in source_file

    @property
    def is_tikz(self) -> bool:
        return self.source_type in TIKZ_SOURCE_TYPES

    @property
    def source(self) -> str:
        """The primary input: the asset for graphics, the TeX file for TikZ."""
        return self.input_asset or self.source_file

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_file": self.source_file,
            "line": self.line,
            "environment": self.environment,
            "label": self.label,
            "input_asset": self.input_asset,
            "source_type": self.source_type,
            "requested_width": self.requested_width,
            "active": self.active,
            "planned_renderer": self.planned_renderer,
            "output": self.output,
            "source_sha256": self.source_sha256,
        }


@dataclass
class FigureRegistry:
    figures: list[FigureRecord] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def active(self) -> list[FigureRecord]:
        return [f for f in self.figures if f.active]

    @property
    def inactive(self) -> list[FigureRecord]:
        return [f for f in self.figures if not f.active]

    def by_id(self, figure_id: str) -> FigureRecord | None:
        return next((f for f in self.figures if f.id == figure_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {"figures": [f.to_dict() for f in self.figures], "findings": [f.to_dict() for f in self.findings]}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _label_in(masked: str, begin: int, end: int) -> str | None:
    for command in iter_commands(masked, begin):
        if command.start >= end:
            return None
        if command.name == "label":
            group = read_group(masked, skip_ws(masked, command.end))
            if group is not None:
                return " ".join(masked[group[0] : group[1]].split()) or None
    return None


@dataclass(frozen=True)
class _ResizeBox:
    width: str
    content_start: int
    content_end: int


def _resize_boxes(masked: str) -> list[_ResizeBox]:
    boxes: list[_ResizeBox] = []
    for command in iter_commands(masked):
        if command.name != "resizebox":
            continue
        groups = []
        pos = command.end
        for _ in range(3):
            group = read_group(masked, skip_ws(masked, pos))
            if group is None:
                break
            groups.append(group)
            pos = group[2]
        if len(groups) == 3:
            boxes.append(_ResizeBox(" ".join(masked[groups[0][0] : groups[0][1]].split()), groups[2][0], groups[2][1]))
    return boxes


def _wrapper_width(masked: str, boxes: list[_ResizeBox], env: Environment) -> str | None:
    """Width of a ``\\resizebox{W}{H}{<env>}`` whose body is exactly ``env``."""
    for box in boxes:
        if box.content_start <= env.begin and env.end <= box.content_end:
            if not masked[box.content_start : env.begin].strip() and not masked[env.end : box.content_end].strip():
                return box.width
    return None


def _renderer_for(source_type: str) -> tuple[str, str]:
    """``(planned renderer, output extension)`` for a source type."""
    if source_type in TIKZ_SOURCE_TYPES:
        return RENDERER_TIKZ, ".svg"
    if source_type == "pdf":
        return RENDERER_PDF, ".svg"
    if source_type == "svg":
        return RENDERER_COPY, ".svg"
    if source_type == "png":
        return RENDERER_COPY, ".png"
    return RENDERER_COPY, ".jpg"


def _figures_in_file(
    scanned: ScannedFile,
    graph: SourceGraph,
    root: Path,
    *,
    active: bool,
    findings: list[Finding],
) -> list[FigureRecord]:
    masked, text = scanned.masked, scanned.text
    floats, float_problems = scan_environments(masked, FLOAT_ENVIRONMENTS)
    tikz, tikz_problems = scan_environments(masked, TIKZ_SOURCE_TYPES)
    severity = Severity.ERROR if active else Severity.INFO
    for problem in [*float_problems, *tikz_problems]:
        findings.append(
            Finding(
                "malformed-environment",
                severity,
                problem.message,
                SourceLocation(scanned.rel, scanned.lines.line(problem.offset)),
            )
        )
    boxes = _resize_boxes(masked)

    items: list[tuple[int, str, Any]] = []
    for env in tikz:
        items.append((env.begin, "tikz", env))
    for edge in graph.edges:
        if edge.source == scanned.rel and edge.kind is EdgeKind.GRAPHIC and edge.resolution is Resolution.OK:
            items.append((edge.offset, "graphic", edge))
    items.sort(key=lambda item: item[0])

    records: list[FigureRecord] = []
    for offset, kind, payload in items:
        enclosing = next((f for f in floats if f.begin <= offset < f.end), None)
        environment = enclosing.name if enclosing else None
        label = _label_in(masked, enclosing.begin, enclosing.end) if enclosing else None
        line = scanned.lines.line(offset)
        if kind == "tikz":
            env: Environment = payload
            source_type = env.name
            asset = None
            width = _wrapper_width(masked, boxes, env)
            digest: str | None = sha256_bytes(text[env.begin : env.end].encode("utf-8"))
            span: tuple[int, int] | None = (env.begin, env.end)
            base = Path(scanned.rel).stem
        else:
            node = graph.nodes[payload.target]
            source_type = node.source_type or "unknown"
            asset = node.id
            width = payload.options.get("width")
            asset_path = root / asset
            digest = sha256_file(asset_path) if asset_path.is_file() else None
            span = None
            base = Path(asset).stem
        renderer, extension = _renderer_for(source_type)
        if source_type == "jpeg" and asset:
            extension = Path(asset).suffix.lower()
        records.append(
            FigureRecord(
                id=base,  # made unique by the caller
                source_file=scanned.rel,
                line=line,
                environment=environment,
                label=label,
                input_asset=asset,
                source_type=source_type,
                requested_width=width,
                active=active,
                planned_renderer=renderer,
                output=extension,  # completed by the caller together with the id
                source_sha256=digest,
                span=span,
            )
        )
    return records


def _orphan_candidates(root: Path, graph: SourceGraph) -> list[str]:
    """Unreachable ``*.tex`` files that sit next to reachable project files."""
    reachable = {n.id for n in graph.nodes.values() if n.resolution is Resolution.OK}
    directories = sorted({str(Path(node_id).parent.as_posix()) for node_id in reachable})
    found: list[str] = []
    for directory in directories:
        folder = root if directory in ("", ".") else root / directory
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if not entry.name.lower().endswith(".tex"):
                continue
            path = Path(entry.path)
            if is_link_like(path) or not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel not in reachable and rel not in found:
                found.append(rel)
    return found


def _assign_ids(records: list[FigureRecord]) -> list[FigureRecord]:
    """Unique, stable ids: sanitised stem; later duplicates get ``-2``, ``-3`` ..."""
    seen: Counter[str] = Counter()
    result: list[FigureRecord] = []
    for record in records:
        base = _LABEL_SAFE.sub("_", record.id) or "figure"
        seen[base] += 1
        figure_id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        result.append(replace(record, id=figure_id, output=f"figures/{figure_id}{record.output}"))
    return result


def discover_figures(graph: SourceGraph, root: Path, main: Path, *, include_inactive: bool = True) -> FigureRegistry:
    """Discover figures of ``graph``; active ones first (document order), then orphans."""
    registry = FigureRegistry()
    records: list[FigureRecord] = []
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is not None:
            records.extend(_figures_in_file(scanned, graph, root, active=True, findings=registry.findings))
    if include_inactive:
        for rel in _orphan_candidates(root, graph):
            isolated = scan_isolated_file(root, main, rel, graph.graphicspath)
            scanned = isolated.files.get(rel)
            if scanned is None:
                continue
            found = _figures_in_file(scanned, isolated, root, active=False, findings=registry.findings)
            records.extend(found)
            # keep the orphan's resolved graphics out of the active graph: nothing to merge
    registry.figures = _assign_ids(records)
    log_event(
        _log,
        logging.INFO,
        "figures discovered",
        active=len(registry.active),
        inactive=len(registry.inactive),
    )
    return registry
