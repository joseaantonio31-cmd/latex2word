"""Source dependency graph of a LaTeX project (read-only).

Starting from the configured main TeX file, :func:`build_source_graph` follows
``\\input``, ``\\include``, ``\\includegraphics``, ``\\graphicspath`` and
``\\bibliography`` using the comment-aware scanner of :mod:`latex2word.latex_scan`.

Resolution follows TeX semantics: relative names are resolved against the
directory of the main file (the TeX working directory), *not* against the file
that mentions them. A dependency that leaves the project root, lexically or
through a symlink/junction, is reported and never read.

Dynamic constructs (macros inside paths, braceless ``\\input``, ``\\subfile`` ...)
produce explicit diagnostics instead of guesses.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from latex2word.diagnostics import Severity
from latex2word.latex_scan import (
    Command,
    LineIndex,
    iter_commands,
    mask_non_live,
    read_group,
    read_optional,
    skip_ws,
    split_top_level,
)
from latex2word.logging_utils import log_event

_log = logging.getLogger(__name__)

MAX_INCLUDE_DEPTH = 64

SUPPORTED_GRAPHIC_EXTENSIONS: dict[str, str] = {
    ".pdf": "pdf",
    ".svg": "svg",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
}
# Order in which an extensionless \includegraphics name is tried.
GRAPHIC_SEARCH_EXTENSIONS: tuple[str, ...] = tuple(SUPPORTED_GRAPHIC_EXTENSIONS)
_KNOWN_UNSUPPORTED_GRAPHIC_EXTENSIONS = frozenset(
    {".eps", ".ps", ".tif", ".tiff", ".gif", ".bmp", ".webp", ".emf", ".wmf", ".jb2", ".jbig2", ".mps", ".ai"}
)
# Structural commands that are recognised but deliberately not followed.
_UNSUPPORTED_COMMANDS: dict[str, str] = {
    "subfile": "\\subfile is not supported; its target is not followed",
    "import": "\\import is not supported; its target is not followed",
    "subimport": "\\subimport is not supported; its target is not followed",
    "includesvg": "\\includesvg is not supported; its target is not followed",
    "includepdf": "\\includepdf is not supported; its target is not followed",
    "InputIfFileExists": "\\InputIfFileExists is not supported; its target is not followed",
    "lstinputlisting": "\\lstinputlisting is not supported; its target is not followed",
    "verbatiminput": "\\verbatiminput is not supported; its target is not followed",
    "includeonly": "\\includeonly changes which \\include files are active; it is ignored",
}
_PGFPLOTS_DATA_COMMANDS = frozenset({"addplot", "pgfplotstableread"})  # ddplot3 is ddplot + "3"
_CLASS_COMMANDS = frozenset({"documentclass", "LoadClass", "LoadClassWithOptions"})
_PACKAGE_COMMANDS = _CLASS_COMMANDS | {"usepackage", "RequirePackage", "RequirePackageWithOptions"}


class NodeKind(str, Enum):
    MAIN = "main"
    TEX = "tex"
    GRAPHIC = "graphic"
    BIBLIOGRAPHY = "bibliography"
    PACKAGE = "package"
    DATA = "data"  # data files read by pgfplots


class EdgeKind(str, Enum):
    INPUT = "input"
    INCLUDE = "include"
    GRAPHIC = "includegraphics"
    BIBLIOGRAPHY = "bibliography"
    PACKAGE = "package"
    DATA = "data"  # data files read by pgfplots


class Resolution(str, Enum):
    OK = "ok"
    MISSING = "missing"
    OUTSIDE_ROOT = "outside-root"
    UNSUPPORTED = "unsupported"
    DYNAMIC = "dynamic"
    CYCLE = "cycle"


@dataclass(frozen=True)
class SourceLocation:
    file: str  # project-relative POSIX path
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line}


@dataclass(frozen=True)
class Finding:
    """One diagnostic of the source graph."""

    code: str
    severity: Severity
    message: str
    location: SourceLocation | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "location": self.location.to_dict() if self.location else None,
        }

    def __str__(self) -> str:
        where = f"{self.location}: " if self.location else ""
        return f"{self.severity.value}: {where}{self.message} [{self.code}]"


@dataclass(frozen=True)
class Node:
    id: str  # project-relative POSIX path (or the normalised reference for unresolved nodes)
    kind: NodeKind
    resolution: Resolution
    source_type: str | None = None  # graphics: pdf/svg/png/jpeg/<other extension>

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "resolution": self.resolution.value,
            "source_type": self.source_type,
        }


@dataclass(frozen=True)
class Edge:
    source: str
    target: str | None
    kind: EdgeKind
    raw: str
    resolution: Resolution
    location: SourceLocation
    options: dict[str, str] = field(default_factory=dict)  # \includegraphics key=value options
    offset: int = 0  # offset of the command in the (unmasked) source text

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": self.source,
            "target": self.target,
            "kind": self.kind.value,
            "raw": self.raw,
            "resolution": self.resolution.value,
            "location": self.location.to_dict(),
        }
        if self.options:
            data["options"] = dict(self.options)
        return data


@dataclass(frozen=True)
class ScannedFile:
    """Text of a reachable TeX file plus its masked form and line index."""

    rel: str
    text: str
    masked: str
    lines: LineIndex


@dataclass
class SourceGraph:
    root: Path
    main: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    tex_order: list[str] = field(default_factory=list)  # pre-order of first visits, main first
    files: dict[str, ScannedFile] = field(default_factory=dict)
    graphicspath: tuple[str, ...] = ()  # final \graphicspath state

    @property
    def tex_files(self) -> list[str]:
        return list(self.tex_order)

    def edges_of(self, *kinds: EdgeKind) -> list[Edge]:
        return [e for e in self.edges if e.kind in kinds]

    def with_resolution(self, *resolutions: Resolution) -> list[Edge]:
        return [e for e in self.edges if e.resolution in resolutions]

    @property
    def missing(self) -> list[Edge]:
        return self.with_resolution(Resolution.MISSING)

    @property
    def outside_root(self) -> list[Edge]:
        return self.with_resolution(Resolution.OUTSIDE_ROOT)

    def resolved_nodes(self, kind: NodeKind) -> list[Node]:
        """Resolved nodes of ``kind`` in first-reference (dependency) order."""
        return [n for n in self.nodes.values() if n.kind is kind and n.resolution is Resolution.OK]

    @property
    def bibliography_files(self) -> list[str]:
        return [n.id for n in self.resolved_nodes(NodeKind.BIBLIOGRAPHY)]

    @property
    def package_files(self) -> list[str]:
        """Project-relative paths of resolved local class/style files, in dependency order."""
        return [n.id for n in self.resolved_nodes(NodeKind.PACKAGE)]

    @property
    def has_errors(self) -> bool:
        return any(f.severity is Severity.ERROR for f in self.findings)

    def stageable_files(self) -> list[tuple[str, str]]:
        """``(relative path, dependency type)`` of every resolved file, in dependency order."""
        kinds = {
            NodeKind.MAIN: "main",
            NodeKind.TEX: "tex",
            NodeKind.GRAPHIC: "graphic",
            NodeKind.BIBLIOGRAPHY: "bibliography",
            NodeKind.PACKAGE: "package",
            NodeKind.DATA: "data",
        }
        return [(n.id, kinds[n.kind]) for n in self.nodes.values() if n.resolution is Resolution.OK]

    def to_dict(self) -> dict[str, Any]:
        return {
            "main": self.main,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class _Probe:
    state: str  # found | absent | outside | link-escape
    rel: str | None  # project-relative POSIX path (inside-root states)
    shown: str  # what to show for the reference in diagnostics
    exists: bool = False


@dataclass(frozen=True)
class _Outcome:
    resolution: Resolution
    node_id: str
    message: str = ""
    abs_path: Path | None = None


def _is_dynamic(value: str) -> bool:
    return any(ch in value for ch in "\\#$")


def _braced(masked: str, pos: int) -> tuple[str, int] | None:
    start = skip_ws(masked, pos)
    group = read_group(masked, start)
    if group is None:
        return None
    return masked[group[0] : group[1]], group[2]


class _Walker:
    def __init__(self, root: Path, main: str, *, follow: bool = True) -> None:
        self.root = Path(root)
        self.root_lex = Path(os.path.normpath(self.root))
        self.root_real = Path(os.path.realpath(self.root))
        self.base = (self.root / main).parent
        self.follow = follow
        self.graph = SourceGraph(self.root, main)
        self.graphicspath: list[str] = []
        self._visited: set[str] = set()
        self._package_files: set[str] = set()
        self._stack: list[str] = []

    # ---- resolution -------------------------------------------------------------------

    def _probe(self, name: str) -> _Probe:
        pure = Path(name)
        candidate = pure if (pure.is_absolute() or pure.anchor) else self.base / pure
        lexical = Path(os.path.normpath(candidate))
        if not lexical.is_relative_to(self.root_lex):
            try:
                shown = Path(os.path.relpath(lexical, self.root_lex)).as_posix()
            except ValueError:  # different drive
                shown = lexical.as_posix()
            return _Probe("outside", None, shown, exists=lexical.exists())
        rel = lexical.relative_to(self.root_lex).as_posix()
        if not lexical.is_file():
            return _Probe("absent", rel, rel)
        if not Path(os.path.realpath(lexical)).is_relative_to(self.root_real):
            return _Probe("link-escape", rel, rel, exists=True)
        return _Probe("found", rel, rel, exists=True)

    def _resolve(self, candidates: list[str]) -> _Outcome:
        probes: list[_Probe] = []
        for name in candidates:
            probe = self._probe(name)
            probes.append(probe)
            if probe.state == "found":
                assert probe.rel is not None
                return _Outcome(Resolution.OK, probe.rel, abs_path=self.root_lex / probe.rel)
            if probe.state == "link-escape":
                return _Outcome(
                    Resolution.OUTSIDE_ROOT, probe.shown, f"{probe.shown} resolves outside the project root through a symlink or junction"
                )
            if probe.state == "outside" and probe.exists:
                return _Outcome(Resolution.OUTSIDE_ROOT, probe.shown, f"{probe.shown} is outside the project root")
        first = probes[0]
        if all(p.state == "outside" for p in probes):
            return _Outcome(Resolution.OUTSIDE_ROOT, first.shown, f"{first.shown} is outside the project root")
        inside = next(p for p in probes if p.state == "absent")
        tried = ", ".join(p.shown for p in probes)
        return _Outcome(Resolution.MISSING, inside.shown, f"not found (tried: {tried})")

    # ---- recording --------------------------------------------------------------------

    def _finding(self, code: str, severity: Severity, message: str, location: SourceLocation | None) -> None:
        self.graph.findings.append(Finding(code, severity, message, location))

    def _node(self, node_id: str, kind: NodeKind, resolution: Resolution, source_type: str | None = None) -> None:
        existing = self.graph.nodes.get(node_id)
        if existing is None or (existing.resolution is not Resolution.OK and resolution is Resolution.OK):
            self.graph.nodes[node_id] = Node(node_id, kind, resolution, source_type)

    def _record_failure(self, outcome: _Outcome, raw: str, location: SourceLocation) -> None:
        if outcome.resolution is Resolution.MISSING:
            self._finding("missing-dependency", Severity.ERROR, f"{raw!r}: {outcome.message}", location)
        elif outcome.resolution is Resolution.OUTSIDE_ROOT:
            self._finding("outside-root", Severity.ERROR, f"{raw!r}: {outcome.message}", location)

    # ---- file scanning ------------------------------------------------------------------

    def _read(self, path: Path, rel: str) -> str | None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._finding("unreadable-file", Severity.ERROR, f"cannot read {rel}: {exc}", None)
            return None
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            self._finding("non-utf8", Severity.WARNING, f"{rel} is not valid UTF-8; decoded as latin-1", None)
            return data.decode("latin-1")

    def scan_file(self, rel: str, path: Path, kind: NodeKind) -> None:
        text = self._read(path, rel)
        if text is None:
            self._node(rel, kind, Resolution.MISSING)
            return
        self._node(rel, kind, Resolution.OK)
        self._visited.add(rel)
        if kind in (NodeKind.MAIN, NodeKind.TEX) and rel not in self.graph.tex_order:
            self.graph.tex_order.append(rel)
        masked_result = mask_non_live(text)
        masked = masked_result.text
        lines = LineIndex(text)
        self.graph.files[rel] = ScannedFile(rel, text, masked, lines)
        for problem in masked_result.problems:
            self._finding(
                "scan-problem", Severity.WARNING, problem.message, SourceLocation(rel, lines.line(problem.offset))
            )
        self._stack.append(rel)
        try:
            if rel in self._package_files:
                for command in iter_commands(masked):
                    if command.name in _PACKAGE_COMMANDS:
                        self._handle_package(command, rel, masked, SourceLocation(rel, lines.line(command.start)))
                return
            for command in iter_commands(masked):
                self._dispatch(command, rel, masked, lines)
        finally:
            self._stack.pop()

    def _dispatch(self, command: Command, rel: str, masked: str, lines: LineIndex) -> None:
        name = command.name
        location = SourceLocation(rel, lines.line(command.start))
        if name in ("input", "include"):
            self._handle_tex_include(command, rel, masked, location)
        elif name == "includegraphics":
            self._handle_graphic(command, rel, masked, location)
        elif name == "graphicspath":
            self._handle_graphicspath(command, masked, location)
        elif name == "bibliography":
            self._handle_bibliography(command, rel, masked, location)
        elif name == "addbibresource":
            self._handle_addbibresource(command, rel, masked, location)
        elif name in _PACKAGE_COMMANDS:
            self._handle_package(command, rel, masked, location)
        elif name in _PGFPLOTS_DATA_COMMANDS:
            self._handle_pgfplots_data(command, rel, masked, location)
        elif name in _UNSUPPORTED_COMMANDS:
            self._finding("unsupported-command", Severity.WARNING, _UNSUPPORTED_COMMANDS[name], location)

    # ---- \input / \include ----------------------------------------------------------------

    def _handle_tex_include(self, command: Command, rel: str, masked: str, location: SourceLocation) -> None:
        kind = EdgeKind.INCLUDE if command.name == "include" else EdgeKind.INPUT
        arg = _braced(masked, command.end)
        if arg is None:
            self._finding(
                "unsupported-syntax",
                Severity.WARNING,
                f"\\{command.name} without a braced argument is not supported; target not followed",
                location,
            )
            return
        raw = " ".join(arg[0].split())
        if not raw or _is_dynamic(raw):
            self._finding(
                "dynamic-path",
                Severity.WARNING,
                f"\\{command.name}{{{raw}}}: path is not a literal; target not followed",
                location,
            )
            self.graph.edges.append(Edge(rel, None, kind, raw, Resolution.DYNAMIC, location, offset=command.start))
            return
        raw = raw.strip('"')
        candidates = [raw] if raw.lower().endswith(".tex") else [raw + ".tex", raw]
        self._note_absolute(raw, location)
        outcome = self._resolve(candidates)
        edge_kwargs = {"offset": command.start}
        if outcome.resolution is not Resolution.OK:
            self._node(outcome.node_id, NodeKind.TEX, outcome.resolution)
            self.graph.edges.append(Edge(rel, outcome.node_id, kind, raw, outcome.resolution, location, **edge_kwargs))
            self._record_failure(outcome, raw, location)
            return
        target = outcome.node_id
        if target in self._stack:
            chain = " -> ".join([*self._stack[self._stack.index(target) :], target])
            self._finding("include-cycle", Severity.ERROR, f"include cycle: {chain}", location)
            self.graph.edges.append(Edge(rel, target, kind, raw, Resolution.CYCLE, location, **edge_kwargs))
            return
        self.graph.edges.append(Edge(rel, target, kind, raw, Resolution.OK, location, **edge_kwargs))
        if not self.follow:
            self._node(target, NodeKind.TEX, Resolution.OK)
            return
        if target in self._visited:
            self._finding(
                "duplicate-reference", Severity.INFO, f"{target} is referenced more than once; scanned once", location
            )
            return
        if len(self._stack) >= MAX_INCLUDE_DEPTH:
            self._finding("max-depth", Severity.ERROR, f"include depth exceeds {MAX_INCLUDE_DEPTH}", location)
            return
        assert outcome.abs_path is not None
        self.scan_file(target, outcome.abs_path, NodeKind.TEX)

    def _note_absolute(self, raw: str, location: SourceLocation) -> None:
        pure = Path(raw)
        if pure.is_absolute() or pure.anchor:
            self._finding("absolute-path", Severity.WARNING, f"{raw!r} is an absolute path; not portable", location)

    # ---- \includegraphics -------------------------------------------------------------------

    def _handle_graphic(self, command: Command, rel: str, masked: str, location: SourceLocation) -> None:
        pos = skip_ws(masked, command.end)
        options: dict[str, str] = {}
        while masked[pos : pos + 1] == "[":
            group = read_optional(masked, pos)
            if group is None:
                self._finding("unsupported-syntax", Severity.WARNING, "unbalanced [ in \\includegraphics", location)
                return
            for item in split_top_level(masked[group[0] : group[1]]):
                key, _, value = item.partition("=")
                options[key.strip()] = value.strip()
            pos = skip_ws(masked, group[2])
        arg = _braced(masked, pos)
        if arg is None:
            self._finding(
                "unsupported-syntax", Severity.WARNING, "\\includegraphics without a braced argument is not supported", location
            )
            return
        raw = " ".join(arg[0].split()).strip('"')
        if not raw or _is_dynamic(raw):
            self._finding(
                "dynamic-path",
                Severity.WARNING,
                f"\\includegraphics{{{raw}}}: path is not a literal; asset not resolved",
                location,
            )
            self.graph.edges.append(
                Edge(rel, None, EdgeKind.GRAPHIC, raw, Resolution.DYNAMIC, location, options, command.start)
            )
            return
        self._note_absolute(raw, location)
        ext = Path(raw).suffix.lower()
        directories = [*self.graphicspath, ""]
        if ext in SUPPORTED_GRAPHIC_EXTENSIONS or ext in _KNOWN_UNSUPPORTED_GRAPHIC_EXTENSIONS:
            candidates = [d + raw for d in directories]
        else:
            candidates = []
            for directory in directories:
                for extension in GRAPHIC_SEARCH_EXTENSIONS:
                    candidates.append(f"{directory}{raw}{extension}")
                    candidates.append(f"{directory}{raw}{extension.upper()}")
                if directory == "":
                    candidates.append(raw)
        outcome = self._resolve(candidates)
        if outcome.resolution is not Resolution.OK:
            self._node(outcome.node_id, NodeKind.GRAPHIC, outcome.resolution)
            self.graph.edges.append(
                Edge(rel, outcome.node_id, EdgeKind.GRAPHIC, raw, outcome.resolution, location, options, command.start)
            )
            self._record_failure(outcome, raw, location)
            return
        found_ext = Path(outcome.node_id).suffix.lower()
        source_type = SUPPORTED_GRAPHIC_EXTENSIONS.get(found_ext)
        resolution = Resolution.OK
        if source_type is None:
            resolution = Resolution.UNSUPPORTED
            source_type = found_ext.lstrip(".") or "unknown"
            self._finding(
                "unsupported-graphic-type",
                Severity.WARNING,
                f"{outcome.node_id}: graphics type {found_ext or '(none)'} is not supported",
                location,
            )
        self._node(outcome.node_id, NodeKind.GRAPHIC, resolution, source_type)
        self.graph.edges.append(
            Edge(rel, outcome.node_id, EdgeKind.GRAPHIC, raw, resolution, location, options, command.start)
        )
        if resolution is Resolution.OK and ext not in SUPPORTED_GRAPHIC_EXTENSIONS:
            self._note_ambiguity(outcome.node_id, location)

    def _note_ambiguity(self, chosen: str, location: SourceLocation) -> None:
        stem = Path(chosen).with_suffix("")
        others = [
            ext
            for ext in GRAPHIC_SEARCH_EXTENSIONS
            if (self.root_lex / stem.with_suffix(ext)).is_file() and stem.with_suffix(ext).as_posix() != chosen
        ]
        if others:
            self._finding(
                "ambiguous-graphic",
                Severity.INFO,
                f"{chosen} chosen; other variants exist: {', '.join(others)}",
                location,
            )

    def _handle_graphicspath(self, command: Command, masked: str, location: SourceLocation) -> None:
        arg = _braced(masked, command.end)
        if arg is None:
            self._finding("unsupported-syntax", Severity.WARNING, "\\graphicspath without a braced argument", location)
            return
        content = arg[0]
        if _is_dynamic(content):
            self._finding(
                "dynamic-path", Severity.WARNING, "\\graphicspath is not literal; graphics search path unchanged", location
            )
            return
        directories: list[str] = []
        pos = skip_ws(content, 0)
        while pos < len(content):
            group = read_group(content, pos)
            if group is None:
                self._finding("unsupported-syntax", Severity.WARNING, "malformed \\graphicspath list", location)
                return
            directory = content[group[0] : group[1]].strip()
            if directory and not directory.endswith("/"):
                directory += "/"
            directories.append(directory)
            pos = skip_ws(content, group[2])
        self.graphicspath = directories
        self.graph.graphicspath = tuple(directories)

    # ---- pgfplots data files ------------------------------------------------------------------

    def _skip_optional(self, masked: str, pos: int) -> int | None:
        if masked[pos : pos + 1] != "[":
            return pos
        group = read_optional(masked, pos)
        return None if group is None else skip_ws(masked, group[2])

    def _handle_pgfplots_data(self, command: Command, rel: str, masked: str, location: SourceLocation) -> None:
        """``\\addplot[3][+][opts] table|file [opts] {path}`` and ``\\pgfplotstableread[opts]{path}``.

        The path is resolved like TeX resolves it: relative to the main file's
        directory. Inline data (``table {x y \\\\ 1 2 \\\\}``), macros and plot
        expressions (``\\addplot {x^2}``) are not file names and are not followed.
        """
        pos: int | None = skip_ws(masked, command.end)
        if command.name != "pgfplotstableread":
            if masked[command.end : command.end + 1] == "3":
                pos = skip_ws(masked, command.end + 1)
            if masked[pos : pos + 1] == "+":
                pos = skip_ws(masked, pos + 1)
            pos = self._skip_optional(masked, pos)
            if pos is None:
                return
            for keyword in ("table", "file"):
                after = pos + len(keyword)
                if masked.startswith(keyword, pos) and not masked[after : after + 1].isalpha():
                    pos = skip_ws(masked, after)
                    break
            else:
                return  # coordinates, an expression or another plot source: no file
        pos = self._skip_optional(masked, pos)
        if pos is None:
            return
        arg = _braced(masked, pos)
        if arg is None:
            return
        name = arg[0].strip()
        if not name or "\\\\" in name or any(ch.isspace() for ch in name):
            return  # inline table data, not a file name
        if name.startswith("\\") and name[1:].isalpha():
            return  # a table macro loaded by \pgfplotstableread, not a path
        if _is_dynamic(name):
            self._finding(
                "dynamic-path", Severity.WARNING, f"\\{command.name} data file {name!r}: path is not a literal", location
            )
            self.graph.edges.append(Edge(rel, None, EdgeKind.DATA, name, Resolution.DYNAMIC, location, offset=command.start))
            return
        self._note_absolute(name, location)
        outcome = self._resolve([name])
        self._node(outcome.node_id, NodeKind.DATA, outcome.resolution)
        self.graph.edges.append(
            Edge(rel, outcome.node_id, EdgeKind.DATA, name, outcome.resolution, location, offset=command.start)
        )
        if outcome.resolution is not Resolution.OK:
            self._record_failure(outcome, name, location)

    # ---- \bibliography -----------------------------------------------------------------------

    def _handle_bibliography(self, command: Command, rel: str, masked: str, location: SourceLocation) -> None:
        arg = _braced(masked, command.end)
        if arg is None:
            self._finding("unsupported-syntax", Severity.WARNING, "\\bibliography without a braced argument", location)
            return
        for name in split_top_level(" ".join(arg[0].split())):
            name = name.strip('"')
            if _is_dynamic(name):
                self._finding(
                    "dynamic-path", Severity.WARNING, f"\\bibliography{{{name}}}: path is not a literal", location
                )
                self.graph.edges.append(
                    Edge(rel, None, EdgeKind.BIBLIOGRAPHY, name, Resolution.DYNAMIC, location, offset=command.start)
                )
                continue
            candidate = name if name.lower().endswith(".bib") else name + ".bib"
            self._note_absolute(name, location)
            outcome = self._resolve([candidate])
            self._node(outcome.node_id, NodeKind.BIBLIOGRAPHY, outcome.resolution)
            self.graph.edges.append(
                Edge(rel, outcome.node_id, EdgeKind.BIBLIOGRAPHY, name, outcome.resolution, location, offset=command.start)
            )
            if outcome.resolution is not Resolution.OK:
                self._record_failure(outcome, name, location)

    def _handle_addbibresource(self, command: Command, rel: str, masked: str, location: SourceLocation) -> None:
        pos = skip_ws(masked, command.end)
        options = ""
        if masked[pos : pos + 1] == "[":
            group = read_optional(masked, pos)
            if group is None:
                self._finding("unsupported-syntax", Severity.WARNING, "unbalanced [ in \\addbibresource", location)
                return
            options = masked[group[0] : group[1]]
            pos = group[2]
        arg = _braced(masked, pos)
        if arg is None:
            self._finding(
                "unsupported-syntax", Severity.WARNING, "\\addbibresource without a braced argument", location
            )
            return
        name = " ".join(arg[0].split()).strip('"')
        if any(item.partition("=")[0].strip() == "location" and item.partition("=")[2].strip() == "remote"
               for item in split_top_level(options)):
            self._finding(
                "remote-bibliography", Severity.INFO, f"\\addbibresource{{{name}}} is a remote resource; not resolved", location
            )
            return
        if not name or _is_dynamic(name):
            self._finding(
                "dynamic-path", Severity.WARNING, f"\\addbibresource{{{name}}}: path is not a literal", location
            )
            self.graph.edges.append(
                Edge(rel, None, EdgeKind.BIBLIOGRAPHY, name, Resolution.DYNAMIC, location, offset=command.start)
            )
            return
        candidate = name if name.lower().endswith(".bib") else name + ".bib"
        self._note_absolute(name, location)
        outcome = self._resolve([candidate])
        self._node(outcome.node_id, NodeKind.BIBLIOGRAPHY, outcome.resolution)
        self.graph.edges.append(
            Edge(rel, outcome.node_id, EdgeKind.BIBLIOGRAPHY, name, outcome.resolution, location, offset=command.start)
        )
        if outcome.resolution is not Resolution.OK:
            self._record_failure(outcome, name, location)

    # ---- \documentclass / \usepackage --------------------------------------------------------

    def _handle_package(self, command: Command, rel: str, masked: str, location: SourceLocation) -> None:
        """Follow a local ``.cls``/``.sty``; names that are not project files are system packages (silent)."""
        suffix = ".cls" if command.name in _CLASS_COMMANDS else ".sty"
        pos = skip_ws(masked, command.end)
        if masked[pos : pos + 1] == "[":
            group = read_optional(masked, pos)
            if group is None:
                return
            pos = group[2]
        arg = _braced(masked, pos)
        if arg is None:
            return
        for name in split_top_level(" ".join(arg[0].split())):
            name = name.strip().strip('"')
            if not name or _is_dynamic(name):
                continue
            candidate = name if name.lower().endswith(suffix) else name + suffix
            probe = self._probe(candidate)
            if probe.state == "absent" or (probe.state == "outside" and not probe.exists):
                continue
            if probe.state != "found":
                message = (
                    f"{probe.shown} resolves outside the project root through a symlink or junction"
                    if probe.state == "link-escape"
                    else f"{probe.shown} is outside the project root"
                )
                outcome = _Outcome(Resolution.OUTSIDE_ROOT, probe.shown, message)
                self._node(outcome.node_id, NodeKind.PACKAGE, outcome.resolution)
                self.graph.edges.append(
                    Edge(rel, outcome.node_id, EdgeKind.PACKAGE, name, outcome.resolution, location, offset=command.start)
                )
                self._record_failure(outcome, name, location)
                continue
            assert probe.rel is not None
            target = probe.rel
            self.graph.edges.append(
                Edge(rel, target, EdgeKind.PACKAGE, name, Resolution.OK, location, offset=command.start)
            )
            self._node(target, NodeKind.PACKAGE, Resolution.OK)
            if not self.follow or target in self._visited:
                continue
            self._package_files.add(target)
            self.scan_file(target, self.root_lex / target, NodeKind.PACKAGE)


def build_source_graph(root: Path, main: Path) -> SourceGraph:
    """Build the dependency graph reachable from ``main`` (an absolute path inside ``root``).

    Read-only: the project is only read, never written. Raises nothing for
    project problems; they are reported as findings and unresolved nodes.
    """
    root = Path(root)
    main_rel = Path(os.path.normpath(main)).relative_to(Path(os.path.normpath(root))).as_posix()
    walker = _Walker(root, main_rel)
    outcome = walker._resolve([str(walker.root_lex / main_rel)])
    graph = walker.graph
    if outcome.resolution is not Resolution.OK:
        walker._node(main_rel, NodeKind.MAIN, Resolution.MISSING)
        graph.findings.append(
            Finding("main-missing", Severity.ERROR, f"main source {main_rel}: {outcome.message or 'not found'}")
        )
        return graph
    assert outcome.abs_path is not None
    walker.scan_file(main_rel, outcome.abs_path, NodeKind.MAIN)
    log_event(
        _log,
        logging.INFO,
        "source graph built",
        tex_files=len(graph.tex_order),
        edges=len(graph.edges),
        errors=sum(1 for f in graph.findings if f.severity is Severity.ERROR),
    )
    return graph


def scan_isolated_file(root: Path, main: Path, rel: str, graphicspath: tuple[str, ...] = ()) -> SourceGraph:
    """Scan one TeX file *without* following includes, resolving graphics like the main project.

    Used to describe unreachable (orphan) files; the result is informational only.
    """
    root = Path(root)
    main_rel = Path(os.path.normpath(main)).relative_to(Path(os.path.normpath(root))).as_posix()
    walker = _Walker(root, main_rel, follow=False)
    walker.graphicspath = list(graphicspath)
    path = walker.root_lex / rel
    if path.is_file() and not Path(os.path.realpath(path)).is_relative_to(walker.root_real):
        return walker.graph  # never read through a link that leaves the project
    if path.is_file():
        walker.scan_file(rel, path, NodeKind.TEX)
    return walker.graph
