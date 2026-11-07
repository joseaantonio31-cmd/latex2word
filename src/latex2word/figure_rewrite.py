"""Figure references in the staged copy: width intermediate representation and rewrites.

M2 produced one asset per active figure. Here the *staged* LaTeX is made to point
at them:

* a TikZ/circuitikz environment (optionally wrapped in ``\\resizebox{W}{!}{...}``)
  becomes ``\\includegraphics[width=W']{_l2w/figures/<id>.svg}``;
* ``\\includegraphics`` of a PDF is re-pointed at the generated SVG;
* PNG/JPEG/SVG graphics stay where they are.

Every ``\\includegraphics`` also gets its ``width=`` normalised. Pandoc's LaTeX
reader understands ``<number>\\textwidth|\\linewidth`` and absolute dimensions
but silently ignores ``\\columnwidth`` and ``.5\\textwidth``; a width it cannot
read would make the image keep its intrinsic size, which depends on the SVG's
internal coordinate scale. A width is therefore parsed into :class:`WidthIR`
first; unsupported expressions are reported, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from latex2word.diagnostics import Severity
from latex2word.figure_pipeline import FiguresRun
from latex2word.latex_scan import (
    iter_commands,
    read_group,
    read_optional,
    scan_environments,
    skip_ws,
)
from latex2word.rewrite import Edit
from latex2word.source_graph import EdgeKind, Finding, Resolution, SourceLocation

ASSET_DIR = "_l2w/figures"  # relative to the directory of the main file, inside the staging tree
_RELATIVE_BASES = ("textwidth", "linewidth", "columnwidth")
_NUMBER = r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
_RELATIVE = re.compile(rf"^({_NUMBER})?\s*\\({'|'.join(_RELATIVE_BASES)})$")
_ABSOLUTE = re.compile(rf"^({_NUMBER})\s*(in|cm|mm|pt|bp|px|pc)$")
_INCHES = {"in": 1.0, "cm": 1 / 2.54, "mm": 1 / 25.4, "pt": 1 / 72.0, "bp": 1 / 72.0, "px": 1 / 96.0, "pc": 1 / 6.0}
# Units Pandoc reads as written; the others are converted to inches before emitting.
_PANDOC_UNITS = ("in", "cm", "mm", "pt", "px")
_IGNORED_OPTIONS = {
    "scale": "scale is not applied by the converter",
    "angle": "rotation is not applied by the converter",
    "trim": "cropping is not applied by the converter",
    "viewport": "cropping is not applied by the converter",
    "clip": "cropping is not applied by the converter",
    "page": "page selection is not applied by the converter",
    "totalheight": "totalheight is not applied by the converter",
}


@dataclass(frozen=True)
class WidthIR:
    """A parsed ``width=`` expression."""

    original: str
    kind: str  # relative | absolute | unsupported
    fraction: float | None = None  # relative: share of the line width
    basis: str | None = None  # relative: textwidth | linewidth | columnwidth
    inches: float | None = None  # absolute: physical width
    emitted: str | None = None  # canonical LaTeX expression handed to Pandoc

    def to_dict(self) -> dict[str, Any]:
        return {
            "original": self.original,
            "kind": self.kind,
            "fraction": self.fraction,
            "basis": self.basis,
            "inches": None if self.inches is None else round(self.inches, 6),
            "emitted": self.emitted,
        }


def _number(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def parse_width(expression: str) -> WidthIR:
    """Parse a LaTeX width such as ``0.98\\columnwidth`` or ``3.5in``."""
    original = " ".join(expression.split())
    relative = _RELATIVE.match(original)
    if relative:
        fraction = float(relative.group(1)) if relative.group(1) else 1.0
        if fraction <= 0:
            return WidthIR(original, "unsupported")
        return WidthIR(
            original, "relative", fraction=fraction, basis=relative.group(2),
            emitted=f"{_number(fraction)}\\linewidth",
        )
    absolute = _ABSOLUTE.match(original)
    if absolute:
        value, unit = float(absolute.group(1)), absolute.group(2)
        if value <= 0:
            return WidthIR(original, "unsupported")
        inches = value * _INCHES[unit]
        emitted = f"{_number(value)}{unit}" if unit in _PANDOC_UNITS else f"{_number(inches)}in"
        return WidthIR(original, "absolute", inches=inches, emitted=emitted)
    return WidthIR(original, "unsupported")


@dataclass(frozen=True)
class FigureRewriteInfo:
    """What the rewrite did for one active figure (feeds the manifest and the validator)."""

    id: str
    source_type: str
    label: str | None
    source_file: str
    line: int
    asset: str  # workspace-relative produced asset
    asset_sha256: str | None
    staged_asset: str | None  # staging-relative path when the figure was re-pointed
    width: WidthIR | None
    aspect: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "label": self.label,
            "source_file": self.source_file,
            "line": self.line,
            "asset": self.asset,
            "asset_sha256": self.asset_sha256,
            "staged_asset": self.staged_asset,
            "width": self.width.to_dict() if self.width else None,
            "aspect": self.aspect,
        }


@dataclass
class FigurePlan:
    edits: list[Edit] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    figures: list[FigureRewriteInfo] = field(default_factory=list)
    assets: list[tuple[str, str]] = field(default_factory=list)  # (workspace-relative source, staging-relative dest)


@dataclass(frozen=True)
class _Include:
    start: int
    end: int
    options: tuple[tuple[int, int, str], ...]  # (start, end, text) of each comma item, in order
    options_span: tuple[int, int] | None  # the inside of the first [...]
    path_span: tuple[int, int]
    path: str


def _parse_include(masked: str, text: str, start: int) -> _Include | None:
    command = next(iter_commands(masked, start), None)
    if command is None or command.name != "includegraphics" or command.start != start:
        return None
    pos = skip_ws(masked, command.end)
    options_span: tuple[int, int] | None = None
    items: list[tuple[int, int, str]] = []
    while masked[pos : pos + 1] == "[":
        group = read_optional(masked, pos)
        if group is None:
            return None
        if options_span is None:
            options_span = (group[0], group[1])
            for piece in _split_with_offsets(masked, group[0], group[1]):
                items.append((piece[0], piece[1], text[piece[0] : piece[1]]))
        pos = skip_ws(masked, group[2])
    path_group = read_group(masked, pos)
    if path_group is None:
        return None
    return _Include(
        start, path_group[2], tuple(items), options_span, (path_group[0], path_group[1]),
        " ".join(text[path_group[0] : path_group[1]].split()),
    )


def _split_with_offsets(masked: str, begin: int, end: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    depth, piece_start, i = 0, begin, begin
    while i < end:
        ch = masked[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            spans.append((piece_start, i))
            piece_start = i + 1
        i += 1
    spans.append((piece_start, end))
    return [(a, b) for a, b in spans if masked[a:b].strip()]


def _find_resizebox(masked: str, begin: int, end: int) -> tuple[int, int, str, str] | None:
    """The ``\\resizebox{W}{H}{<env>}`` whose body is exactly the environment ``[begin, end)``."""
    for command in iter_commands(masked):
        if command.name != "resizebox" or command.start > begin:
            continue
        groups = []
        pos = command.end
        for _ in range(3):
            group = read_group(masked, skip_ws(masked, pos))
            if group is None:
                break
            groups.append(group)
            pos = group[2]
        if len(groups) != 3:
            continue
        body_start, body_end, after = groups[2]
        if body_start <= begin and end <= body_end and not masked[body_start:begin].strip() and not masked[end:body_end].strip():
            width = " ".join(masked[groups[0][0] : groups[0][1]].split())
            height = " ".join(masked[groups[1][0] : groups[1][1]].split())
            return command.start, after, width, height
    return None


def _find_resizebox_around_input(graph: Any, rel: str, begin: int, end: int) -> tuple[str, int, int, str, str, str] | None:
    """``\\resizebox{W}{H}{\\input{rel}}`` in a file that includes ``rel``.

    Only when the environment ``[begin, end)`` is the whole live content of
    ``rel``, so that the box scales exactly that drawing. Returns the including
    file, the span of the resizebox there, its width and height, and the
    input command text that replaces it.
    """
    scanned = graph.files.get(rel)
    if scanned is None or scanned.masked[:begin].strip() or scanned.masked[end:].strip():
        return None
    for edge in graph.edges:
        if edge.target != rel or edge.kind not in (EdgeKind.INPUT, EdgeKind.INCLUDE) or edge.offset is None:
            continue
        if edge.resolution is not Resolution.OK:
            continue
        parent = graph.files.get(edge.source)
        if parent is None:
            continue
        masked = parent.masked
        arg = read_group(masked, skip_ws(masked, iter_end(masked, edge.offset)))
        if arg is None:
            continue
        wrapper = _find_resizebox(masked, edge.offset, arg[2])
        if wrapper is not None:
            start, stop, width, height = wrapper
            return edge.source, start, stop, width, height, parent.text[edge.offset : arg[2]]
    return None


def iter_end(masked: str, offset: int) -> int:
    """End of the control word starting at ``offset`` (a backslash)."""
    pos = offset + 1
    while pos < len(masked) and masked[pos].isalpha():
        pos += 1
    return pos


def _aspect(outcome: Any) -> float | None:
    metrics = outcome.report.metrics if outcome.report else None
    if metrics is None:
        return None
    width = getattr(metrics, "width_px", None)
    height = getattr(metrics, "height_px", None)
    if width and height:
        return float(width) / float(height)
    return None


def _width_findings(ir: WidthIR, where: SourceLocation, subject: str) -> list[Finding]:
    if ir.kind == "unsupported":
        return [
            Finding(
                "L2W-W-FIG-003", Severity.WARNING,
                f"{subject}: width expression {ir.original!r} is not supported; the image keeps its intrinsic size",
                where,
            )
        ]
    return []


def _rewrite_options(
    text: str, include: _Include, new_path: str | None, where: SourceLocation, subject: str,
    findings: list[Finding],
) -> tuple[str, WidthIR | None]:
    """The new ``\\includegraphics[...]{...}`` text and the parsed width of the original."""
    parts: list[str] = []
    width_ir: WidthIR | None = None
    for _, _, item in include.options:
        key, equals, value = item.partition("=")
        key = key.strip()
        if key == "width" and equals:
            width_ir = parse_width(value)
            findings.extend(_width_findings(width_ir, where, subject))
            parts.append(f"width={width_ir.emitted}" if width_ir.emitted else item.strip())
        else:
            if key in _IGNORED_OPTIONS:
                findings.append(
                    Finding("L2W-W-FIG-004", Severity.WARNING, f"{subject}: option {item.strip()!r}: "
                            f"{_IGNORED_OPTIONS[key]}", where)
                )
            parts.append(item.strip())
    path = new_path if new_path is not None else text[include.path_span[0] : include.path_span[1]]
    options = f"[{','.join(parts)}]" if parts else ""
    return f"\\includegraphics{options}{{{path}}}", width_ir


def plan_figure_rewrites(run: FiguresRun, main_rel: str) -> FigurePlan:
    """Plan the staged-copy edits for ``run`` (a successful figures run).

    ``main_rel`` is the project-relative path of the main file; generated assets
    are staged next to it under :data:`ASSET_DIR`.
    """
    plan = FigurePlan()
    graph = run.inspection.graph
    main_dir = PurePosixPath(main_rel).parent
    prefix = "" if str(main_dir) in ("", ".") else f"{main_dir.as_posix()}/"
    by_asset: dict[tuple[str, str], list[Any]] = {}
    tikz_by_file: dict[str, list[Any]] = {}
    for outcome in run.outcomes:
        record = outcome.record
        if record.is_tikz:
            tikz_by_file.setdefault(record.source_file, []).append(outcome)
        elif record.input_asset:
            by_asset.setdefault((record.source_file, record.input_asset), []).append(outcome)

    def stage_asset(outcome: Any, record: Any) -> str:
        extension = PurePosixPath(record.output).suffix
        relative = f"{ASSET_DIR}/{record.id}{extension}"
        plan.assets.append((record.output, f"{prefix}{relative}"))
        return relative

    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked, text = scanned.masked, scanned.text
        # 1. TikZ / circuitikz environments
        for outcome in sorted(tikz_by_file.get(rel, []), key=lambda o: o.record.span[0]):
            record = outcome.record
            assert record.span is not None
            begin, end = record.span
            where = SourceLocation(rel, scanned.lines.line(begin))
            wrapper = _find_resizebox(masked, begin, end)
            width_text, start, stop = None, begin, end
            outer = None if wrapper is not None else _find_resizebox_around_input(graph, rel, begin, end)
            if outer is not None:
                # \resizebox{W}{H}{\input{file}} in the including file: unwrap it there and
                # carry W to the generated image (the drawing is the whole file)
                parent_rel, outer_start, outer_stop, width_text, height, input_text = outer
                plan.edits.append(
                    Edit(parent_rel, outer_start, outer_stop, input_text, "figure-tikz",
                         f"\\resizebox around \\input of a {record.source_type} file unwrapped; its width is "
                         "applied to the generated SVG", record.id)
                )
                wrapper = (begin, end, width_text, height)
            if wrapper is not None:
                start, stop, width_text, height = wrapper
                if height not in ("!", ""):
                    plan.findings.append(
                        Finding("L2W-W-FIG-006", Severity.WARNING,
                                f"{record.id}: \\resizebox height {height!r} is ignored; the aspect ratio is preserved", where)
                    )
            ir = parse_width(width_text) if width_text else None
            if ir is not None:
                plan.findings.extend(_width_findings(ir, where, record.id))
            staged = stage_asset(outcome, record)
            options = f"[width={ir.emitted}]" if ir is not None and ir.emitted else ""
            replacement = f"\\includegraphics{options}{{{staged}}}"
            plan.edits.append(
                Edit(
                    rel, start, stop, replacement, "figure-tikz",
                    f"{record.source_type} environment replaced by its generated SVG "
                    "(Pandoc cannot render TikZ)",
                    record.id,
                )
            )
            plan.figures.append(
                FigureRewriteInfo(
                    record.id, record.source_type, record.label, rel, where.line, record.output,
                    outcome.output_sha256, f"{prefix}{staged}", ir, _aspect(outcome),
                )
            )
        # 2. \includegraphics commands (PDF re-pointing and width normalisation for all)
        queues = {key: list(value) for key, value in by_asset.items() if key[0] == rel}
        drawings = [o.record.span for o in tikz_by_file.get(rel, []) if o.record.span is not None]
        for edge in graph.edges:
            if edge.source != rel or edge.kind is not EdgeKind.GRAPHIC or edge.resolution is not Resolution.OK:
                continue
            if any(begin <= edge.offset < end for begin, end in drawings):
                continue  # part of a drawing replaced above as a whole; TeX already applied its options
            include = _parse_include(masked, text, edge.offset)
            if include is None:
                continue
            node = graph.nodes.get(edge.target or "")
            source_type = node.source_type if node else None
            where = SourceLocation(rel, scanned.lines.line(edge.offset))
            outcome = None
            queue = queues.get((rel, edge.target or ""))
            if queue:
                outcome = queue.pop(0)
            subject = outcome.record.id if outcome else (edge.target or edge.raw)
            new_path = None
            staged_rel: str | None = None
            if source_type == "pdf" and outcome is not None:
                staged_rel = stage_asset(outcome, outcome.record)
                new_path = staged_rel
            findings: list[Finding] = []
            replacement, ir = _rewrite_options(text, include, new_path, where, subject, findings)
            plan.findings.extend(findings)
            original = text[include.start : include.end]
            if replacement != original:
                kinds = []
                if new_path is not None:
                    kinds.append(("figure-asset", "PDF graphic replaced by its generated SVG (Pandoc embeds raster/SVG only)"))
                if ir is not None and ir.emitted and ir.emitted != ir.original.replace(" ", ""):
                    kinds.append(("figure-width", "width expression normalised to a form Pandoc understands"))
                kind, reason = kinds[0] if kinds else ("figure-width", "option text normalised")
                if len(kinds) > 1:
                    kind, reason = "figure-asset", "; ".join(r for _, r in kinds)
                plan.edits.append(Edit(rel, include.start, include.end, replacement, kind, reason, subject))
            if outcome is not None:
                record = outcome.record
                plan.figures.append(
                    FigureRewriteInfo(
                        record.id, record.source_type, record.label, rel, where.line, record.output,
                        outcome.output_sha256, f"{prefix}{staged_rel}" if staged_rel else record.input_asset, ir,
                        _aspect(outcome),
                    )
                )
    plan.figures.sort(key=lambda f: [o.record.id for o in run.outcomes].index(f.id))
    return plan


def remaining_tikz(graph_files: dict[str, str]) -> list[tuple[str, int]]:
    """TikZ/circuitikz environments still present in rewritten staged text (``file -> text``)."""
    from latex2word.latex_scan import LineIndex, mask_non_live

    found: list[tuple[str, int]] = []
    for rel, text in graph_files.items():
        masked = mask_non_live(text).text
        environments, _ = scan_environments(masked, ("tikzpicture", "circuitikz"))
        index = LineIndex(text)
        found.extend((rel, index.line(env.begin)) for env in environments)
    return found


def copy_assets(plan: FigurePlan, workspace: Path, staging: Path) -> None:
    """Copy the generated assets into the staged tree (byte-for-byte)."""
    import shutil

    for source, destination in plan.assets:
        target = staging / Path(*PurePosixPath(destination).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(workspace / Path(*PurePosixPath(source).parts), target)


__all__ = [
    "ASSET_DIR",
    "FigurePlan",
    "FigureRewriteInfo",
    "WidthIR",
    "copy_assets",
    "parse_width",
    "plan_figure_rewrites",
    "remaining_tikz",
]
