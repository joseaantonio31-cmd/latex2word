"""EXPERIMENTAL (audit R3): structural confinement of Pandoc's LaTeX reader.

Not used by the production build unless ``LATEX2WORD_EXPERIMENTAL_CONFINED_READER=1`` is set
(see ``build.run_build``). Design and evidence: ``reports/audit/remediation-phase2.md``.

Why: latex2word's scanner and Pandoc's reader interpret TeX differently (``\\iffalse`` nesting,
``filecontents``, aliases...), so a deny-list over the scanner cannot prove what Pandoc will read.
The structural answer splits the Pandoc run in two:

1. **flatten** (this module): the staged, already validated source graph is expanded into ONE
   file. Every ``\\input``/``\\include`` that latex2word resolved and staged is replaced by the staged
   text; every local ``.sty`` that latex2word resolved is inlined into the preamble. A source map
   keeps the original file and offset of every flattened character.
2. **confined reader**: ``pandoc --sandbox -f latex -t json flat.tex``. Under ``--sandbox`` the reader
   may read only the files named on its command line (observed with Pandoc 3.1.3: ``\\input``,
   ``\\include``, ``\\usepackage{local}``, ``\\lstinputlisting``, ``\\inputminted``, ``\\subfile`` are all
   refused). Every refused read is reported as ``L2W-E-CONFINE-001``: it is content that did not come
   from the validated graph.
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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from latex2word.diagnostics import Severity
from latex2word.latex_scan import iter_commands, read_group, read_optional, skip_ws
from latex2word.process import CommandResult, run_command
from latex2word.source_graph import EdgeKind, Finding, Resolution, SourceGraph, SourceLocation

FLATTENED_NAME = "l2w-flattened.tex"
_INCLUDE_WARNING = re.compile(r"Could not load include file (.+?) at \S+ line (\d+) column (\d+)")


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


def _command_span(graph: SourceGraph, rel: str, offset: int, kind: EdgeKind) -> tuple[int, int, str]:
    """``(start, end, argument)`` of the include/package command whose backslash is at ``offset``."""
    masked = graph.files[rel].masked
    command = next(iter_commands(masked, offset), None)
    if command is None or command.start != offset:
        raise FlattenError(f"{rel}: no command at offset {offset}")
    pos = skip_ws(masked, command.end)
    if kind is EdgeKind.PACKAGE:
        while masked[pos : pos + 1] == "[":
            optional = read_optional(masked, pos)
            if optional is None:
                raise FlattenError(f"{rel}: unbalanced options in \\{command.name}")
            pos = skip_ws(masked, optional[2])
    group = read_group(masked, pos)
    if group is None:
        raise FlattenError(f"{rel}: \\{command.name} without a braced argument cannot be flattened")
    return command.start, group[2], masked[group[0] : group[1]]


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
        position = 0
        handled: set[int] = set()
        for edge in edges:
            if edge.offset in handled:
                raise FlattenError(f"{rel}:{edge.location.line}: \\usepackage with several local packages in one list")
            handled.add(edge.offset)
            start, end, argument = _command_span(graph, rel, edge.offset, edge.kind)
            if edge.kind is EdgeKind.PACKAGE:
                if not edge.target.lower().endswith(".sty"):
                    continue  # local classes are not loaded by Pandoc either: keep \documentclass as written
                if "," in argument:
                    raise FlattenError(f"{rel}:{edge.location.line}: \\usepackage{{{argument}}} mixes a local package with others")
            emit_text(text[position:start], rel, position)
            if edge.kind is EdgeKind.PACKAGE:
                emit_text("\\makeatletter\n", None, 0)
                emit_file(edge.target, (*stack, rel))
                emit_text("\n\\makeatother\n", None, 0)
            else:
                emit_file(edge.target, (*stack, rel))
                if not graph.files[edge.target].text.endswith("\n"):
                    emit_text("\n", None, 0)
            position = end
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
) -> ConfinedRead:
    """Run the confined reader on ``flat`` (written to ``workdir``); refused reads become errors."""
    source = workdir / FLATTENED_NAME
    source.write_text(flat.text, encoding="utf-8", newline="\n")
    argv = [pandoc, "--sandbox", "--from", "latex", "--to", "json", FLATTENED_NAME]
    result = runner(argv, timeout=timeout_s, cwd=workdir)
    findings: list[Finding] = []
    line_starts = [0] + [i + 1 for i, ch in enumerate(flat.text) if ch == "\n"]
    for match in _INCLUDE_WARNING.finditer(result.stderr):
        line, column = int(match.group(2)), int(match.group(3))
        offset = line_starts[line - 1] + column - 1 if 0 < line <= len(line_starts) else -1
        findings.append(Finding(
            "L2W-E-CONFINE-001", Severity.ERROR,
            f"the confined reader refused to read {match.group(1)!r}: content that does not come from the validated, "
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


def check_ast_resources(ast: dict[str, Any], base: Path, staging: Path) -> list[Finding]:
    """Every ``Image`` target must be a relative path to an existing file inside ``staging``."""
    findings: list[Finding] = []
    staging_real = os.path.realpath(staging)
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
            real = os.path.realpath(base / Path(*pure.parts))
            if not (real == staging_real or real.startswith(staging_real.rstrip(os.sep) + os.sep)):
                problem = "resolves outside the staged project"
            elif not os.path.isfile(real):
                problem = "does not exist in the staged project"
        if problem:
            findings.append(Finding("L2W-E-CONFINE-002", Severity.ERROR,
                                    f"image target {target!r} {problem}; the DOCX writer would fetch it"))
    return findings
