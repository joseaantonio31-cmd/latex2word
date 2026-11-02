"""Structural confinement of Pandoc's LaTeX reader (audit R3): the production read path.

``build.run_build`` always reads the LaTeX this way; ``LATEX2WORD_UNSAFE_LEGACY_READER=1`` restores the
single unconfined Pandoc run for diagnosis only (warning ``L2W-W-CONFINE-005``). Design and evidence:
``reports/audit/remediation-phase2.md``.

Why: latex2word's scanner and Pandoc's reader interpret TeX differently (``\\iffalse`` nesting,
``filecontents``, aliases...), so a deny-list over the scanner cannot prove what Pandoc will read.
The structural answer splits the Pandoc run in two:

1. **flatten** (this module): the staged, already validated source graph is expanded into ONE
   file. Every ``\\input``/``\\include`` that latex2word resolved and staged is replaced by the staged
   text; every local ``.sty`` that latex2word resolved is inlined into the preamble (a package list
   ``\\usepackage[o]{a,mine,b}`` becomes ``\\usepackage[o]{a}``, inlined ``mine.sty``, ``\\usepackage[o]{b}``).
   A source map keeps the original file and offset of every flattened character.
2. **confined reader**: ``pandoc --sandbox --verbose -f latex -t json [profile pandoc.extra_args] flat.tex``
   (the profile's arguments reach the reader as in a single-pass run; one that needs a file is refused by the
   sandbox and fails the build). Under ``--sandbox`` the
   reader may read only the files named on its command line (observed with Pandoc 3.1.3, 3.12 and 3.13:
   ``\\input``, ``\\include``, ``\\usepackage{local}``, ``\\lstinputlisting``, ``\\inputminted``, ``\\subfile``
   are all refused). Every refused read is reported as ``L2W-E-CONFINE-001``: it is content that did not
   come from the validated graph. A refused ``\\usepackage`` is logged only at INFO level, like every
   system package, so it is an error only when the file exists in the staged project or the name is an
   absolute path (an unconfined run would have read it).
3. **resource check**: the JSON AST is structured data; every ``Image`` target must be a relative path
   to an existing file inside the staging directory (``L2W-E-CONFINE-002``), because the DOCX writer
   fetches images by path.
4. **writer** (unchanged latex2word filters, citeproc, reference document): ``pandoc -f json -t docx``.
   It never interprets TeX. It is NOT sandboxed: with this Pandoc build ``--sandbox`` also blocks the
   writer's own data files ("Could not find data file data/data/docx/[Content_Types].xml"), and Lua
   filters are not confined by ``--sandbox`` at all (observed: ``io.open`` reads any file). latex2word's
   filters are trusted code that read only the build's own configuration.

What this does not cover: TeX execution for TikZ figures (separate process, F-07).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from latex2word.diagnostics import Severity
from latex2word.latex_scan import iter_commands, read_group, read_optional, skip_ws, split_top_level
from latex2word.process import CommandResult, run_command
from latex2word.source_graph import EdgeKind, Finding, Resolution, SourceGraph, SourceLocation

FLATTENED_NAME = "l2w-flattened.tex"
_INCLUDE_WARNING = re.compile(
    r"^\[(?P<level>WARNING|INFO)\] Could not load include file (?P<name>.+?) at \S+ line (?P<line>\d+) column (?P<column>\d+)",
    re.MULTILINE,
)


class FlattenError(Exception):
    """A construct that cannot be flattened safely: the build must fail loudly instead."""


@dataclass(frozen=True)
class Segment:
    """``flat[start:end]`` is ``file[file_start:file_start + (end - start)]``."""

    start: int
    end: int
    file: str
    file_start: int


@dataclass
class FlatSource:
    text: str
    segments: list[Segment] = field(default_factory=list)

    def locate(self, offset: int, graph: SourceGraph) -> SourceLocation | None:
        """The original ``file:line`` of flattened offset ``offset`` (``None`` for inserted glue text)."""
        for segment in self.segments:
            if segment.start <= offset < segment.end:
                scanned = graph.files.get(segment.file)
                if scanned is None:
                    return SourceLocation(segment.file, None)
                return SourceLocation(segment.file, scanned.lines.line(segment.file_start + offset - segment.start))
        return None


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    argument: str
    name: str  # command name without the backslash
    options: str  # the option groups as written, brackets included ("" when there are none)


def _command_span(graph: SourceGraph, rel: str, offset: int, kind: EdgeKind) -> _Span:
    """The include/package command whose backslash is at ``offset``."""
    scanned = graph.files[rel]
    masked = scanned.masked
    command = next(iter_commands(masked, offset), None)
    if command is None or command.start != offset:
        raise FlattenError(f"{rel}: no command at offset {offset}")
    pos = skip_ws(masked, command.end)
    options_start = options_end = pos
    if kind is EdgeKind.PACKAGE:
        while masked[pos : pos + 1] == "[":
            optional = read_optional(masked, pos)
            if optional is None:
                raise FlattenError(f"{rel}: unbalanced options in \\{command.name}")
            options_end = optional[2]
            pos = skip_ws(masked, optional[2])
    group = read_group(masked, pos)
    if group is None:
        raise FlattenError(f"{rel}: \\{command.name} without a braced argument cannot be flattened")
    return _Span(command.start, group[2], masked[group[0] : group[1]], command.name,
                 scanned.text[options_start:options_end])


def _package_names(argument: str) -> list[str]:
    """The names of a package list, split exactly as the source graph splits them (``Edge.raw``)."""
    return [name for name in (n.strip().strip('"') for n in split_top_level(" ".join(argument.split()))) if name]


def flatten(graph: SourceGraph) -> FlatSource:
    """Expand the resolved ``\\input``/``\\include`` files and local ``.sty`` packages of ``graph`` into one text."""
    flat = FlatSource("")
    parts: list[str] = []
    cursor = 0

    def emit_text(text: str, rel: str | None, file_start: int) -> None:
        nonlocal cursor
        if not text:
            return
        if rel is not None:
            flat.segments.append(Segment(cursor, cursor + len(text), rel, file_start))
        parts.append(text)
        cursor += len(text)

    def emit_file(rel: str, stack: tuple[str, ...]) -> None:
        if rel in stack:
            raise FlattenError(f"include cycle through {rel}")
        if len(stack) > 32:
            raise FlattenError("include depth exceeds 32")
        scanned = graph.files.get(rel)
        if scanned is None:
            raise FlattenError(f"{rel} is in the graph but was not scanned")
        text = scanned.text
        edges = sorted(
            (e for e in graph.edges
             if e.source == rel and e.kind in (EdgeKind.INPUT, EdgeKind.INCLUDE, EdgeKind.PACKAGE)
             and e.resolution is Resolution.OK and e.target is not None),
            key=lambda e: e.offset,
        )
        commands: dict[int, list[Any]] = {}  # one command may carry several package edges (a package list)
        for edge in edges:
            commands.setdefault(edge.offset, []).append(edge)
        position = 0
        for offset, group in commands.items():
            first = group[0]
            span = _command_span(graph, rel, offset, first.kind)
            if first.kind is EdgeKind.PACKAGE:
                local = {e.raw: e for e in group if e.target.lower().endswith(".sty")}
                if not local:
                    continue  # local classes are not loaded by Pandoc either: keep \documentclass as written
                names = _package_names(span.argument)
                if not set(local) <= set(names):
                    raise FlattenError(f"{rel}:{first.location.line}: \\{span.name}{{{span.argument}}} cannot be split safely")
                emit_text(text[position : span.start], rel, position)
                # \usepackage[opts]{a,mine,b} -> \usepackage[opts]{a} <mine.sty inlined> \usepackage[opts]{b}: the load
                # order is kept; system packages stay commands (Pandoc only reacts to the names it knows)
                for name in names:
                    edge = local.get(name)
                    if edge is None:
                        emit_text(f"\\{span.name}{span.options}{{{name}}}\n", None, 0)
                    else:
                        emit_text("\\makeatletter\n", None, 0)
                        emit_file(edge.target, (*stack, rel))
                        emit_text("\n\\makeatother\n", None, 0)
            else:
                if len(group) > 1:
                    raise FlattenError(f"{rel}:{first.location.line}: several inputs share one command")
                emit_text(text[position : span.start], rel, position)
                emit_file(first.target, (*stack, rel))
                if not graph.files[first.target].text.endswith("\n"):
                    emit_text("\n", None, 0)
            position = span.end
        emit_text(text[position:], rel, position)

    emit_file(graph.main, ())
    flat.text = "".join(parts)
    return flat


Runner = Callable[..., CommandResult]


@dataclass
class ConfinedRead:
    ast: dict[str, Any] | None
    findings: list[Finding]
    argv: tuple[str, ...]
    stderr: str


def read_confined(
    pandoc: str, flat: FlatSource, graph: SourceGraph, workdir: Path, *, timeout_s: float, runner: Runner = run_command,
    extra_args: Sequence[str] = (),
) -> ConfinedRead:
    """Run the confined reader on ``flat`` (written to ``workdir``); refused reads become errors."""
    source = workdir / FLATTENED_NAME
    source.write_text(flat.text, encoding="utf-8", newline="\n")
    # --verbose: a refused \usepackage is reported only at INFO level (Pandoc 3.12/3.13), so without it a local package
    # the flattener did not inline would vanish silently
    argv = [pandoc, "--sandbox", "--verbose", "--from", "latex", "--to", "json", *extra_args, FLATTENED_NAME]
    result = runner(argv, timeout=timeout_s, cwd=workdir)
    findings: list[Finding] = []
    line_starts = [0] + [i + 1 for i, ch in enumerate(flat.text) if ch == "\n"]
    # where an unconfined Pandoc would have looked for package files: the staged main file's directory
    base = Path(graph.root) / Path(*PurePosixPath(graph.main).parent.parts)
    for match in _INCLUDE_WARNING.finditer(result.stderr):
        if match.group("level") == "INFO":
            # every \usepackage is reported at INFO level, system packages too (amsmath.sty is not a project file);
            # only a file an unconfined run would really have read is lost content
            name = match.group("name")
            if not (Path(name).is_absolute() or PurePosixPath(name).is_absolute() or (base / name).is_file()):
                continue
        line, column = int(match.group("line")), int(match.group("column"))
        offset = line_starts[line - 1] + column - 1 if 0 < line <= len(line_starts) else -1
        findings.append(Finding(
            "L2W-E-CONFINE-001", Severity.ERROR,
            f"the confined reader refused to read {match.group('name')!r}: content that does not come from the validated, "
            "flattened source is not converted",
            flat.locate(offset, graph) if offset >= 0 else None,
        ))
    if not result.ok:
        findings.append(Finding("L2W-E-CONFINE-003", Severity.ERROR,
                                f"the confined reader failed: {result.error or result.stderr.strip()[:200] or result.returncode}"))
        return ConfinedRead(None, findings, tuple(argv), result.stderr)
    try:
        ast = json.loads(result.stdout)
    except ValueError:
        findings.append(Finding("L2W-E-CONFINE-003", Severity.ERROR, "the confined reader did not produce a JSON AST"))
        return ConfinedRead(None, findings, tuple(argv), result.stderr)
    return ConfinedRead(ast, findings, tuple(argv), result.stderr)


def _walk(node: Any):
    if isinstance(node, dict):
        if "t" in node:
            yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def resource_dirs(graph: SourceGraph, base: Path) -> list[Path]:
    """The image search path of a single-pass Pandoc run: ``base`` (the main file's directory), then the
    ``\\graphicspath`` directories. The LaTeX reader keeps the latter only in its own state, which the JSON AST does
    not carry, so the separate writer run must be given them explicitly (``--resource-path``)."""
    dirs = [base]
    for entry in graph.graphicspath:
        directory = base / Path(*PurePosixPath(entry.replace("\\", "/")).parts)
        if directory not in dirs:
            dirs.append(directory)
    return dirs


def check_ast_resources(ast: dict[str, Any], base: Path, staging: Path, search: list[Path] | None = None) -> list[Finding]:
    """Every ``Image`` target must be a relative path to an existing file inside ``staging``.

    ``search`` is the writer's resource path (default ``[base]``): the file the writer fetches is the first match in
    that order, and that file must lie inside ``staging``.
    """
    findings: list[Finding] = []
    staging_real = os.path.realpath(staging)
    dirs = search or [base]
    for element in _walk(ast):
        if element.get("t") != "Image":
            continue
        target = element["c"][2][0]
        pure = PurePosixPath(target.replace("\\", "/"))
        problem = None
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) and not re.match(r"^[a-zA-Z]:[\\/]", target):
            problem = "is a URL"
        elif pure.is_absolute() or re.match(r"^[a-zA-Z]:", target):
            problem = "is an absolute path"
        else:
            candidates = [os.path.realpath(d / Path(*pure.parts)) for d in dirs]
            real = next((c for c in candidates if os.path.isfile(c)), candidates[0])
            if not (real == staging_real or real.startswith(staging_real.rstrip(os.sep) + os.sep)):
                problem = "resolves outside the staged project"
            elif not os.path.isfile(real):
                problem = "does not exist in the staged project"
        if problem:
            findings.append(Finding("L2W-E-CONFINE-002", Severity.ERROR,
                                    f"image target {target!r} {problem}; the DOCX writer would fetch it"))
    return findings
