"""Bibliography architecture: modes, source detection, key verification, staged rewrites.

Modes
-----
``citeproc``  external ``.bib`` files rendered by Pandoc's citeproc with a CSL style;
``manual``    an inline ``thebibliography`` kept as numbered entries;
``auto``      pick whichever source exists; both present without explicit
              precedence is an *ambiguity* error, never a silent choice.

Pandoc facts this module is built on (docs/pandoc-ast-findings.md): the LaTeX
reader collapses a ``thebibliography`` into one paragraph and loses every key,
so an inline bibliography is rewritten in the staged copy into one
``l2wbibitem`` per ``\\bibitem`` that the Lua filter numbers; ``\\cite``-family
commands become ``Cite`` nodes that vanish silently unless citeproc (or the
filter) renders them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from latex2word.config import BibliographySection
from latex2word.diagnostics import Severity
from latex2word.latex_scan import iter_commands, read_group, read_optional, scan_environments, skip_ws, split_top_level
from latex2word.rewrite import Edit
from latex2word.source_graph import EdgeKind, Finding, Resolution, SourceGraph, SourceLocation

MODES = ("citeproc", "manual", "none")
_CITE_NAME = re.compile(r"^[A-Za-z]*cite[A-Za-z]*$", re.I)
_NOT_CITATIONS = frozenset({"citestyle", "citeindextrue", "citeindexfalse", "bibliographystyle", "citepunct", "citeleft", "citeright"})
_MULTI_GROUP = re.compile(r"^[A-Za-z]*cites$", re.I)
_BIB_ENTRY = re.compile(r"^[ \t]*@([A-Za-z]+)[ \t]*[{(][ \t]*([^,\s{}()]+)[ \t]*,", re.M)
_NON_ENTRY_TYPES = frozenset({"string", "comment", "preamble"})
BIB_KEY_PREFIX = "l2w-bib-"


@dataclass(frozen=True)
class CitationUse:
    key: str
    command: str
    location: SourceLocation


@dataclass(frozen=True)
class BibEntryKeys:
    file: str
    keys: tuple[str, ...]  # in file order
    duplicates: tuple[str, ...]


@dataclass
class BibliographyPlan:
    mode: str = "none"  # citeproc | manual | none
    reason: str = ""
    files: list[str] = field(default_factory=list)  # project-relative .bib files (citeproc)
    csl: Path | None = None
    heading: str = ""
    cited: list[CitationUse] = field(default_factory=list)
    nocite_all: bool = False
    defined: set[str] = field(default_factory=set)
    inline_keys: list[str] = field(default_factory=list)
    expected_entries: int | None = None
    edits: list[Edit] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    sources: dict[str, Any] = field(default_factory=dict)

    @property
    def cited_keys(self) -> list[str]:
        seen: dict[str, None] = {}
        for use in self.cited:
            if use.key != "*":
                seen.setdefault(use.key, None)
        return list(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "files": list(self.files),
            "csl": self.csl.name if self.csl else None,
            "heading": self.heading,
            "cited_keys": self.cited_keys,
            "nocite_all": self.nocite_all,
            "inline_keys": list(self.inline_keys),
            "expected_entries": self.expected_entries,
            "sources": self.sources,
        }


def parse_bib_keys(text: str) -> tuple[list[str], list[str]]:
    """``(keys in file order, duplicate keys)`` of a BibTeX/BibLaTeX file."""
    keys: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for match in _BIB_ENTRY.finditer(text):
        if match.group(1).lower() in _NON_ENTRY_TYPES:
            continue
        key = match.group(2)
        if key in seen:
            duplicates.append(key)
        else:
            seen.add(key)
            keys.append(key)
    return keys, duplicates


def scan_citations(graph: SourceGraph) -> list[CitationUse]:
    """Every citation key used in live text of the reachable TeX files, in document order."""
    uses: list[CitationUse] = []
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked = scanned.masked
        for command in iter_commands(masked):
            name = command.name
            if not _CITE_NAME.match(name) or name.lower() in _NOT_CITATIONS:
                continue
            pos = skip_ws(masked, command.end)
            while masked[pos : pos + 1] == "[":
                group = read_optional(masked, pos)
                if group is None:
                    break
                pos = skip_ws(masked, group[2])
            location = SourceLocation(rel, scanned.lines.line(command.start))
            while True:
                group = read_group(masked, pos)
                if group is None:
                    break
                for key in split_top_level(masked[group[0] : group[1]]):
                    uses.append(CitationUse(" ".join(key.split()), name, location))
                if not _MULTI_GROUP.match(name):
                    break
                pos = skip_ws(masked, group[2])
    return uses


@dataclass(frozen=True)
class _BibItem:
    key: str
    label: str | None
    text: str
    line: int


def _inline_environments(graph: SourceGraph) -> list[tuple[str, Any]]:
    found = []
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        environments, _ = scan_environments(scanned.masked, ("thebibliography",))
        found.extend((rel, env) for env in environments)
    return found


def _parse_items(rel: str, graph: SourceGraph, env: Any) -> tuple[list[_BibItem], list[Finding]]:
    scanned = graph.files[rel]
    masked, text = scanned.masked, scanned.text
    start = skip_ws(masked, env.content_start)
    width = read_group(masked, start)  # the {widest label} argument
    body_start = width[2] if width else env.content_start
    marks = [c for c in iter_commands(masked, body_start) if c.start < env.content_end and c.name == "bibitem"]
    items: list[_BibItem] = []
    findings: list[Finding] = []
    for index, command in enumerate(marks):
        pos = skip_ws(masked, command.end)
        label: str | None = None
        optional = read_optional(masked, pos)
        if optional is not None:
            label = text[optional[0] : optional[1]].strip()
            pos = skip_ws(masked, optional[2])
        key_group = read_group(masked, pos)
        where = SourceLocation(rel, scanned.lines.line(command.start))
        if key_group is None:
            findings.append(Finding("L2W-E-BIB-004", Severity.ERROR, "\\bibitem without a key cannot be cited", where))
            continue
        key = " ".join(masked[key_group[0] : key_group[1]].split())
        stop = marks[index + 1].start if index + 1 < len(marks) else env.content_end
        items.append(_BibItem(key, label, text[key_group[2] : stop].strip(), where.line))
        if label:
            findings.append(
                Finding("L2W-W-BIB-006", Severity.WARNING,
                        f"\\bibitem[{label}]{{{key}}}: a custom label is not supported; entries are numbered", where)
            )
    return items, findings


def _command_span(masked: str, start: int) -> tuple[int, int] | None:
    """Span of ``\\bibliography{..}`` / ``\\addbibresource[..]{..}`` starting at ``start``."""
    command = next(iter_commands(masked, start), None)
    if command is None or command.start != start or command.name not in ("bibliography", "addbibresource"):
        return None
    pos = skip_ws(masked, command.end)
    while masked[pos : pos + 1] == "[":
        group = read_optional(masked, pos)
        if group is None:
            return None
        pos = skip_ws(masked, group[2])
    group = read_group(masked, pos)
    return (start, group[2]) if group else None


def _hyper_text(item: _BibItem) -> str:
    return f"\\begin{{l2wbibitem}}\\hypertarget{{{BIB_KEY_PREFIX}{item.key}}}{{{item.text}}}\\end{{l2wbibitem}}"


def plan_bibliography(
    graph: SourceGraph,
    config: BibliographySection,
    profile_bibliography: Mapping[str, Any],
    profile_csl: Path | None,
    project_root: Path,
) -> BibliographyPlan:
    """Decide the bibliography mode, verify keys and plan the staged-copy rewrites."""
    plan = BibliographyPlan()
    plan.heading = profile_bibliography.get("heading", "")
    plan.cited = scan_citations(graph)
    plan.nocite_all = any(use.key == "*" for use in plan.cited)
    declared_mode = config.mode or profile_bibliography.get("mode", "auto")
    prefer = config.prefer or profile_bibliography.get("prefer")

    source_files = graph.bibliography_files
    external = list(config.files) if config.files else list(source_files)
    inline_envs = _inline_environments(graph)
    plan.sources = {
        "declared_mode": declared_mode,
        "prefer": prefer,
        "source_bibliography_files": list(source_files),
        "configured_bibliography_files": list(config.files),
        "inline_environments": [rel for rel, _ in inline_envs],
    }
    has_external, has_inline = bool(external), bool(inline_envs)
    uses_citations = bool(plan.cited)

    def error(code: str, message: str, where: SourceLocation | None = None) -> None:
        plan.findings.append(Finding(code, Severity.ERROR, message, where))

    mode = declared_mode
    if declared_mode == "auto":
        if has_external and has_inline:
            if prefer == "external":
                mode, plan.reason = "citeproc", "both sources exist; configuration prefers the external bibliography"
            elif prefer == "inline":
                mode, plan.reason = "manual", "both sources exist; configuration prefers the inline bibliography"
            else:
                mode = "none"
                error(
                    "L2W-E-BIB-001",
                    "ambiguous bibliography: the source has an inline thebibliography and external bibliography "
                    f"files ({', '.join(external)}); set bibliography.mode or bibliography.prefer",
                )
        elif has_external:
            mode, plan.reason = "citeproc", "external bibliography files found"
        elif has_inline:
            mode, plan.reason = "manual", "inline thebibliography found"
        else:
            mode, plan.reason = "none", "no bibliography source"
            if uses_citations:
                error("L2W-E-BIB-002", "the document cites works but has no bibliography source "
                      "(\\bibliography, \\addbibresource, thebibliography or bibliography.files)")
    else:
        plan.reason = "mode set explicitly"
        if declared_mode == "citeproc" and not has_external:
            mode = "none"
            error("L2W-E-BIB-002", "bibliography mode 'citeproc' needs bibliography files "
                  "(\\bibliography, \\addbibresource or bibliography.files)")
        if declared_mode == "manual" and not has_inline:
            mode = "none"
            error("L2W-E-BIB-002", "bibliography mode 'manual' needs an inline thebibliography environment")
    plan.mode = mode

    if mode == "citeproc":
        _plan_citeproc(plan, graph, external, config, profile_csl, project_root, inline_envs)
    elif mode == "manual":
        _plan_manual(plan, graph, inline_envs)
    if plan.mode != "none":
        _check_keys(plan)
    return plan


def _plan_citeproc(
    plan: BibliographyPlan,
    graph: SourceGraph,
    external: list[str],
    config: BibliographySection,
    profile_csl: Path | None,
    project_root: Path,
    inline_envs: list[tuple[str, Any]],
) -> None:
    plan.files = external
    for rel in external:
        path = project_root / Path(*PurePosixPath(rel).parts)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            plan.findings.append(Finding("L2W-E-BIB-009", Severity.ERROR, f"cannot read bibliography file {rel}: {exc}"))
            continue
        keys, duplicates = parse_bib_keys(text)
        plan.defined.update(keys)
        for key in duplicates:
            plan.findings.append(
                Finding("L2W-W-BIB-009", Severity.WARNING, f"{rel}: duplicate bibliography key {key!r}; the first entry is used")
            )
        if not keys:
            plan.findings.append(Finding("L2W-W-BIB-010", Severity.WARNING, f"{rel} defines no entries"))
    csl = config.csl or profile_csl
    if csl is not None and not csl.is_file():
        plan.findings.append(Finding("L2W-E-BIB-013", Severity.ERROR, f"CSL style file does not exist: {csl.name}"))
    elif csl is None:
        plan.findings.append(
            Finding("L2W-I-BIB-007", Severity.INFO, "no CSL style configured; Pandoc's default citation style is used")
        )
    plan.csl = csl
    # The staged copy lists the files explicitly: drop the source's own commands (one authority).
    seen: set[tuple[str, int]] = set()
    for edge in graph.edges:
        if edge.kind is not EdgeKind.BIBLIOGRAPHY or edge.resolution is not Resolution.OK:
            continue
        scanned = graph.files.get(edge.source)
        if scanned is None or (edge.source, edge.offset) in seen:
            continue
        seen.add((edge.source, edge.offset))
        span = _command_span(scanned.masked, edge.offset)
        if span is not None:
            plan.edits.append(
                Edit(edge.source, span[0], span[1], "", "bibliography-remove",
                     "bibliography files are passed to citeproc explicitly; the source command is removed from the staged copy")
            )
    for rel, env in inline_envs:
        plan.edits.append(
            Edit(rel, env.begin, env.end, "", "bibliography-remove",
                 "inline thebibliography ignored: bibliography mode is citeproc")
        )
        plan.findings.append(
            Finding("L2W-W-BIB-005", Severity.WARNING, "an inline thebibliography is ignored (external bibliography selected)",
                    SourceLocation(rel, graph.files[rel].lines.line(env.begin)))
        )


def _plan_manual(plan: BibliographyPlan, graph: SourceGraph, inline_envs: list[tuple[str, Any]]) -> None:
    for rel, env in inline_envs:
        items, findings = _parse_items(rel, graph, env)
        plan.findings.extend(findings)
        for item in items:
            if item.key in plan.defined:
                plan.findings.append(
                    Finding("L2W-E-BIB-003", Severity.ERROR, f"duplicate \\bibitem key {item.key!r}",
                            SourceLocation(rel, item.line))
                )
                continue
            plan.defined.add(item.key)
            plan.inline_keys.append(item.key)
        body = "\n".join(_hyper_text(item) for item in items)
        plan.edits.append(
            Edit(rel, env.begin, env.end, f"\\begin{{l2wbibliography}}\n{body}\n\\end{{l2wbibliography}}",
                 "bibliography-inline",
                 "Pandoc collapses thebibliography into one paragraph and drops the keys; "
                 "each \\bibitem becomes an l2wbibitem the filter numbers")
        )
    if len(inline_envs) > 1:
        plan.findings.append(
            Finding("L2W-W-BIB-012", Severity.WARNING, f"{len(inline_envs)} thebibliography environments found; entries are numbered across all of them")
        )
    for edge in graph.edges:
        if edge.kind is EdgeKind.BIBLIOGRAPHY and edge.resolution is Resolution.OK:
            scanned = graph.files.get(edge.source)
            span = _command_span(scanned.masked, edge.offset) if scanned else None
            if span is not None:
                plan.edits.append(
                    Edit(edge.source, span[0], span[1], "", "bibliography-remove",
                         "external bibliography ignored: bibliography mode is manual")
                )
    plan.expected_entries = len(plan.inline_keys)


def _check_keys(plan: BibliographyPlan) -> None:
    missing: dict[str, CitationUse] = {}
    for use in plan.cited:
        if use.key == "*":
            continue
        if use.key not in plan.defined:
            missing.setdefault(use.key, use)
    for key, use in missing.items():
        plan.findings.append(
            Finding("L2W-E-CITE-001", Severity.ERROR,
                    f"citation key {key!r} (\\{use.command}) is not defined in the bibliography", use.location)
        )
    if plan.mode == "citeproc":
        if plan.nocite_all:
            plan.expected_entries = len(plan.defined)
        else:
            plan.expected_entries = len({k for k in plan.cited_keys if k in plan.defined})
        uncited = len(plan.defined) - len({k for k in plan.cited_keys if k in plan.defined})
        if uncited > 0 and not plan.nocite_all:
            plan.findings.append(
                Finding("L2W-I-BIB-008", Severity.INFO, f"{uncited} bibliography entr{'y' if uncited == 1 else 'ies'} "
                        "never cited; they are not listed")
            )
    elif plan.mode == "manual":
        plan.expected_entries = len(plan.inline_keys)
