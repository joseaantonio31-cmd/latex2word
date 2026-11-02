"""Find LaTeX constructs that Pandoc would silently discard.

Pandoc's LaTeX reader drops commands it does not know *together with their
arguments* (``\\IEEEauthorblockN{Name}`` leaves no trace); only with the
``raw_tex`` extension do they surface as raw nodes, but that extension also
stops ``\\input`` from being followed. So the document itself is converted
normally and this module asks Pandoc a narrower question: it builds a tiny probe
document with one paragraph per distinct command/environment used in the
document body and converts it with ``+raw_tex``. A command that comes back as a
raw node is unknown to Pandoc; the document's real arguments then decide the
severity (prose lost vs. a harmless formatting switch).

Known approximations (reported as such, never hidden): macros defined by the
document or its local ``.sty`` files are trusted; commands that Pandoc only
handles inside a specific context are covered by a small allow-list.

Policy (fail-closed): an unknown command whose arguments carry prose is an ERROR (``L2W-E-TEX-001``)
unless it is known-safe (Pandoc understands it, or it is defined by the document/local style), accepted
by the profile, consumed by a metadata adapter, or on the project's explicit allow-list. Citations and
references that Pandoc would lose are always errors; switches without text are informational.
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from latex2word.diagnostics import Severity
from latex2word.latex_scan import iter_commands, read_group, read_optional, skip_ws
from latex2word.logging_utils import log_event
from latex2word.pandoc_adapter import PandocTool
from latex2word.process import run_command
from latex2word.source_graph import EdgeKind, Finding, NodeKind, Resolution, SourceGraph, SourceLocation
from latex2word.toolrun import Runner, run_logged

_log = logging.getLogger(__name__)

# Handled by Pandoc only in a specific context (so a bare probe paragraph cannot show it) or by latex2word itself.
CONTEXTUAL = frozenset(
    {
        "item", "caption", "label", "bibitem", "hline", "toprule", "midrule", "bottomrule", "cline", "cmidrule",
        "multicolumn", "multirow", "endhead", "endfirsthead", "endfoot", "endlastfoot", "ref", "eqref", "autoref",
        "cref", "Cref", "vref", "includegraphics", "footnotetext", "footnotemark", "hypertarget", "hyperref", "href", "url",
        "maketitle", "begin", "end", "input", "include", "par", "newline", "linebreak", "tabularnewline",
        "l2wbibliography", "l2wbibitem", "title", "author", "date", "abstract", "and", "thanks",
    }
)
# Generic LaTeX layout commands and length registers: they carry no text, so dropping them is expected
# and never reported (this list is about LaTeX itself, not about any publisher).
NON_CONTENT = frozenset(
    {
        "bibliographystyle", "bibliography", "addbibresource", "printbibliography", "pagestyle", "thispagestyle",
        "pagenumbering", "clearpage", "newpage", "cleardoublepage", "noindent", "indent", "smallskip", "medskip",
        "bigskip", "vfill", "hfill", "centering", "raggedright", "raggedleft", "raggedbottom", "flushbottom",
        "tiny", "scriptsize", "footnotesize", "small", "normalsize", "large", "Large", "LARGE", "huge", "Huge",
        "bfseries", "itshape", "mdseries", "upshape", "rmfamily", "sffamily", "ttfamily", "normalfont", "selectfont",
        "linewidth", "textwidth", "columnwidth", "textheight", "paperwidth", "paperheight", "hsize", "baselineskip",
        "tabcolsep", "arraystretch", "parindent", "parskip", "evensidemargin", "oddsidemargin", "unitlength",
        "appendix", "frontmatter", "mainmatter", "backmatter", "balance", "protect", "relax", "ignorespaces",
        "vspace", "hspace", "vskip", "hskip", "quad", "qquad", "allowbreak", "nobreak", "nolinebreak", "nopagebreak", "pagebreak", "enlargethispage", "FloatBarrier",
    }
)
# Environments Pandoc handles but cannot show in a bare probe paragraph (they need arguments or a context).
KNOWN_ENVIRONMENTS = frozenset(
    {
        "tabular", "tabular*", "tabularx", "tabulary", "longtable", "array", "minipage", "table", "table*", "figure",
        "figure*", "center", "flushleft", "flushright", "itemize", "enumerate", "description", "quote", "quotation",
        "verse", "verbatim", "abstract", "document", "thebibliography", "l2wbibliography", "l2wbibitem", "subfigure",
        "wrapfigure", "sidewaystable", "sidewaysfigure", "appendices", "titlepage", "l2wappendix",
    }
)
# Entries for LaTeX's own contents/figure/table lists. Word regenerates its TOC from the headings, so the
# text of a manual entry only ever reached a LaTeX list: reported (info), never an error.
TOC_ONLY = frozenset({"addcontentsline", "addtocontents"})
# Reference commands whose effect latex2word reproduces from the profile instead of the command.
HANDLED_REFERENCE_SETTINGS = {
    "crefalias": "reference names after \\appendix come from the profile (numbering.sections.appendix)",
}
# Commands whose loss changes meaning (a reference or citation vanishes).
SEMANTIC_LOSS = {
    "nameref": "the named reference text",
    "Nameref": "the named reference text",
    "pageref": "the page number",
    "vpageref": "the page number",
}
_DEFINERS = re.compile(
    r"\\(?:re)?(?:newcommand|providecommand)\*?\s*\{?\s*\\([A-Za-z@]+)"
    r"|\\(?:DeclareRobustCommand|DeclareMathOperator|def|gdef|edef|xdef|let|newif)\*?\s*\{?\s*\\([A-Za-z@]+)"
    r"|\\(?:re)?newenvironment\*?\s*\{([A-Za-z0-9@*]+)\}"
    r"|\\newtheorem\*?\s*\{([A-Za-z0-9@*]+)\}"
)
_DEFINITION_COMMANDS = frozenset(
    {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand", "DeclareMathOperator", "def", "gdef",
     "edef", "xdef", "let", "newif", "newenvironment", "renewenvironment", "newtheorem", "setlength", "setcounter",
     "addtolength", "addtocounter", "usepackage", "RequirePackage", "documentclass", "usetikzlibrary", "hypersetup"}
)
_MATH_ENVIRONMENTS = frozenset(
    {"equation", "equation*", "align", "align*", "alignat", "alignat*", "flalign", "flalign*", "gather", "gather*",
     "multline", "multline*", "eqnarray", "eqnarray*", "displaymath", "math", "aligned", "split", "array", "cases",
     "matrix", "pmatrix", "bmatrix", "vmatrix", "Vmatrix", "Bmatrix", "smallmatrix", "gathered", "alignedat"}
)
_RAW_COMMAND = re.compile(r"^\\([A-Za-z@]+)")
_RAW_BEGIN = re.compile(r"^\\begin\s*\{([^{}]+)\}")
_PROSE = re.compile(r"[A-Za-z]{3,}")
_DIMENSION = re.compile(r"^[\s0-9.+\-*/]*(?:pt|cm|mm|in|em|ex|bp|px|pc|sp|fil+)?\s*$")


@dataclass
class _Usage:
    kind: str  # command | environment
    name: str
    location: SourceLocation
    count: int = 0
    prose: bool = False
    prose_location: SourceLocation | None = None
    has_arguments: bool = False


@dataclass
class ProbeResult:
    findings: list[Finding] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    pandoc_run: dict[str, Any] | None = None


def _blank(segment: str) -> str:
    return re.sub(r"[^\n]", " ", segment)


def mask_math(masked: str) -> str:
    """Blank math regions (``$..$``, ``$$..$$``, ``\\(..\\)``, ``\\[..\\]``, math environments)."""
    out: list[str] = []
    n, i, last = len(masked), 0, 0

    def cut(start: int, stop: int) -> None:
        nonlocal last
        out.append(masked[last:start])
        out.append(_blank(masked[start:stop]))
        last = stop

    while i < n:
        ch = masked[i]
        if ch == "\\":
            nxt = masked[i + 1 : i + 2]
            if nxt in ("(", "["):
                closer = "\\)" if nxt == "(" else "\\]"
                end = masked.find(closer, i + 2)
                end = n if end == -1 else end + 2
                cut(i, end)
                i = end
                continue
            match = re.compile(r"\\begin\s*\{([^{}]+)\}").match(masked, i)
            if match and match.group(1) in _MATH_ENVIRONMENTS:
                name = match.group(1)
                end_match = re.compile(r"\\end\s*\{" + re.escape(name) + r"\}").search(masked, match.end())
                end = end_match.end() if end_match else n
                cut(i, end)
                i = end
                continue
            i += 2
            continue
        if ch == "$":
            if masked[i + 1 : i + 2] == "$":
                end = masked.find("$$", i + 2)
                end = n if end == -1 else end + 2
            else:
                j = i + 1
                while j < n and masked[j] != "$":
                    j += 2 if masked[j] == "\\" else 1
                end = min(n, j + 1)
            cut(i, end)
            i = end
            continue
        i += 1
    out.append(masked[last:])
    return "".join(out)


def defined_names(graph: SourceGraph) -> set[str]:
    """Macros/environments defined by the document or by local ``.sty`` files (which Pandoc loads)."""
    names: set[str] = set()
    for rel, scanned in graph.files.items():
        node = graph.nodes.get(rel)
        if node is not None and node.kind is NodeKind.PACKAGE and rel.lower().endswith(".cls"):
            continue  # Pandoc does not load document classes
        for match in _DEFINERS.finditer(scanned.masked):
            names.update(g for g in match.groups() if g)
    return names


def _preamble_files(graph: SourceGraph) -> set[str]:
    """TeX files included before ``\\begin{document}`` (macro files and the like)."""
    main = graph.files.get(graph.main)
    if main is None:
        return set()
    match = re.search(r"\\begin\s*\{document\}", main.masked)
    limit = match.start() if match else 0
    preamble: set[str] = set()
    stack = [
        e.target for e in graph.edges
        if e.source == graph.main and e.kind in (EdgeKind.INPUT, EdgeKind.INCLUDE)
        and e.resolution is Resolution.OK and e.offset < limit and e.target
    ]
    while stack:
        rel = stack.pop()
        if rel in preamble:
            continue
        preamble.add(rel)
        stack.extend(
            e.target for e in graph.edges
            if e.source == rel and e.kind in (EdgeKind.INPUT, EdgeKind.INCLUDE) and e.resolution is Resolution.OK and e.target
        )
    return preamble


def _body_range(rel: str, masked: str, graph: SourceGraph) -> tuple[int, int]:
    if rel != graph.main:
        return 0, len(masked)
    begin = re.search(r"\\begin\s*\{document\}", masked)
    end = re.search(r"\\end\s*\{document\}", masked)
    return (begin.end() if begin else 0), (end.start() if end else len(masked))


def _skip_arguments(masked: str, pos: int) -> int:
    """Skip the argument groups of a definition-like command (up to the last consecutive group)."""
    pos = skip_ws(masked, pos)
    for _ in range(6):
        if masked[pos : pos + 1] == "[":
            group = read_optional(masked, pos)
        elif masked[pos : pos + 1] == "{":
            group = read_group(masked, pos)
        else:
            break
        if group is None:
            break
        pos = skip_ws(masked, group[2])
    return pos


def _argument_text(masked: str, text: str, pos: int) -> tuple[bool, bool]:
    """``(has_arguments, prose)`` for the arguments following a command at ``pos``."""
    pos = skip_ws(masked, pos) if masked[pos : pos + 1] in "[{" or masked[pos : pos + 1].isspace() else pos
    has_args, prose = False, False
    for _ in range(4):
        if masked[pos : pos + 1] == "[":
            group = read_optional(masked, pos)
        elif masked[pos : pos + 1] == "{":
            group = read_group(masked, pos)
        else:
            break
        if group is None:
            break
        has_args = True
        content = re.sub(r"\\[A-Za-z@]+\*?", " ", text[group[0] : group[1]])
        if _PROSE.search(content) and not _DIMENSION.match(content):
            prose = True
        pos = group[2]
    return has_args, prose


def collect_usage(graph: SourceGraph, known: Collection[str]) -> "OrderedDict[tuple[str, str], _Usage]":
    """Distinct commands/environments of the live, non-math document body."""
    usage: OrderedDict[tuple[str, str], _Usage] = OrderedDict()
    skipped = _preamble_files(graph)
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None or rel in skipped:
            continue
        start, stop = _body_range(rel, scanned.masked, graph)
        body = mask_math(scanned.masked)
        text = scanned.text
        position = start
        for command in iter_commands(body, start):
            if command.start < position:
                continue
            if command.start >= stop:
                break
            name = command.name
            if name in _DEFINITION_COMMANDS:
                position = _skip_arguments(body, command.end)
                continue
            where = SourceLocation(rel, scanned.lines.line(command.start))
            if name in ("begin", "end"):
                group = read_group(body, skip_ws(body, command.end))
                if group is None or name == "end":
                    continue
                env = " ".join(body[group[0] : group[1]].split())
                if env in KNOWN_ENVIRONMENTS or env in known:
                    continue
                key = ("environment", env)
                if key not in usage:
                    usage[key] = _Usage("environment", env, where)
                usage[key].count += 1
                continue
            if not name.isalpha() and name != "@":
                continue
            if name == "includegraphics":
                position = _skip_arguments(body, command.end)
                continue
            if name in known or name in CONTEXTUAL or name in NON_CONTENT:
                continue
            key = ("command", name)
            entry = usage.setdefault(key, _Usage("command", name, where))
            entry.count += 1
            has_args, prose = _argument_text(body, text, command.end)
            entry.has_arguments = entry.has_arguments or has_args
            if prose and not entry.prose:
                entry.prose, entry.prose_location = True, where
    return usage


def _probe_document(usage: "OrderedDict[tuple[str, str], _Usage]") -> str:
    lines = ["\\documentclass{article}", "\\begin{document}", ""]
    for kind, name in usage:
        if kind == "command":
            lines += [f"\\{name}{{a}}{{b}}", ""]
        else:
            lines += [f"\\begin{{{name}}}x\\end{{{name}}}", ""]
    lines.append("\\end{document}")
    return "\n".join(lines) + "\n"


def _raw_names(node: Any, found: set[tuple[str, str]]) -> None:
    if isinstance(node, dict):
        kind = node.get("t")
        if kind in ("RawInline", "RawBlock"):
            content = node.get("c", [])
            if isinstance(content, list) and len(content) == 2 and content[0] in ("latex", "tex"):
                text = content[1]
                begin = _RAW_BEGIN.match(text)
                if begin:
                    found.add(("environment", begin.group(1).strip()))
                else:
                    match = _RAW_COMMAND.match(text)
                    if match:
                        found.add(("command", match.group(1)))
            return
        if kind == "Cite":
            return  # the fallback raw text of a recognised citation is not an unknown command
        for value in node.values():
            _raw_names(value, found)
    elif isinstance(node, list):
        for value in node:
            _raw_names(value, found)


def probe(
    graph: SourceGraph,
    tool: PandocTool,
    workdir: Path,
    *,
    accepted_commands: Collection[str] = (),
    accepted_environments: Collection[str] = (),
    consumed_commands: Collection[str] = (),
    consumed_environments: Collection[str] = (),
    allowed_commands: Collection[str] = (),
    allowed_environments: Collection[str] = (),
    timeout_s: float = 60.0,
    runner: Runner = run_command,
) -> ProbeResult:
    """Probe the document's constructs. ``workdir`` must be a latex2word-owned directory.

    ``consumed_*`` names were handled by a metadata adapter, ``allowed_*`` names come from the project's
    explicit allow-list and ``accepted_*`` names from the profile; see the module docstring for the policy.
    """
    result = ProbeResult()
    known = defined_names(graph)
    usage = collect_usage(graph, known)
    result.names = [f"{kind}:{name}" for kind, name in usage]
    if not usage:
        return result
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "probe.tex").write_bytes(_probe_document(usage).encode("utf-8"))
    run = run_logged(
        [tool.path, "--from", "latex+raw_tex", "--to", "json", "probe.tex", "--output", "probe.json"],
        cwd=workdir, timeout=timeout_s, runner=runner,
    )
    result.pandoc_run = {"argv": list(run.argv), "exit_code": run.returncode, "stderr": run.stderr}
    if not run.ok:
        result.findings.append(
            Finding("L2W-W-TEX-004", Severity.WARNING,
                    f"the unknown-construct probe failed ({run.failure_reason()}); unknown LaTeX commands may be dropped silently")
        )
        return result
    try:
        raw: set[tuple[str, str]] = set()
        _raw_names(json.loads((workdir / "probe.json").read_text(encoding="utf-8")), raw)
    except (OSError, ValueError) as exc:
        result.findings.append(Finding("L2W-W-TEX-004", Severity.WARNING, f"the unknown-construct probe output is unreadable: {exc}"))
        return result
    accepted_c, accepted_e = set(accepted_commands), set(accepted_environments)
    policy = _Policy(
        accepted_c, accepted_e, set(consumed_commands), set(consumed_environments),
        set(allowed_commands), set(allowed_environments),
    )
    for key, entry in usage.items():
        if key not in raw:
            continue
        result.unknown.append(f"{key[0]}:{key[1]}")
        result.findings.append(_classify(entry, policy))
    log_event(_log, logging.INFO, "tex probe finished", constructs=len(usage), unknown=len(result.unknown))
    return result


@dataclass(frozen=True)
class _Policy:
    """The names the probe must not treat as dropped text, by the reason they are acceptable."""

    accepted_commands: set[str]
    accepted_environments: set[str]
    consumed_commands: set[str]
    consumed_environments: set[str]
    allowed_commands: set[str]
    allowed_environments: set[str]


def _classify_environment(entry: _Usage, policy: _Policy, suffix: str) -> Finding:
    name, where = entry.name, entry.location
    if name in policy.consumed_environments:
        return Finding("L2W-I-TEX-004", Severity.INFO, f"environment {name!r} is handled by the metadata adapter{suffix}", where)
    if name in policy.accepted_environments:
        return Finding("L2W-I-TEX-003", Severity.INFO, f"environment {name!r} accepted by the profile{suffix}", where)
    if name in policy.allowed_environments:
        return Finding("L2W-I-TEX-003", Severity.INFO, f"environment {name!r} allowed by project configuration{suffix}", where)
    return Finding(
        "L2W-I-TEX-002", Severity.INFO,
        f"environment {name!r} is unknown to Pandoc; its text is kept in a generic block{suffix}", where,
    )


def _classify(entry: _Usage, policy: _Policy) -> Finding:
    name, where, times = entry.name, entry.location, entry.count
    suffix = f" ({times} occurrence{'s' if times != 1 else ''})" if times > 1 else ""
    if entry.kind == "environment":
        return _classify_environment(entry, policy, suffix)
    lowered = name.lower()
    if name in policy.consumed_commands:
        return Finding("L2W-I-TEX-004", Severity.INFO, f"\\{name} is handled by the metadata adapter{suffix}", where)
    if name in TOC_ONLY:
        return Finding(
            "L2W-I-TEX-005", Severity.INFO,
            f"\\{name}: a manual entry of a LaTeX contents list; Word builds its table of contents from the headings, "
            f"so the entry text is not carried over{suffix}", where,
        )
    if name in HANDLED_REFERENCE_SETTINGS:
        return Finding("L2W-I-TEX-006", Severity.INFO, f"\\{name}: {HANDLED_REFERENCE_SETTINGS[name]}{suffix}", where)
    if name in SEMANTIC_LOSS:
        return Finding(
            "L2W-E-REF-006", Severity.ERROR,
            f"\\{name} is not supported: {SEMANTIC_LOSS[name]} would silently vanish{suffix}", where,
        )
    if "cite" in lowered:
        return Finding("L2W-E-CITE-002", Severity.ERROR, f"unsupported citation command \\{name}{suffix}", where)
    if "ref" in lowered:
        return Finding("L2W-E-REF-006", Severity.ERROR, f"unsupported reference command \\{name}{suffix}", where)
    if name in policy.accepted_commands:
        return Finding("L2W-I-TEX-003", Severity.INFO, f"command \\{name} accepted by the profile{suffix}", where)
    if name in policy.allowed_commands:
        return Finding("L2W-I-TEX-003", Severity.INFO, f"command \\{name} allowed by project configuration{suffix}", where)
    if entry.prose:
        return Finding(
            "L2W-E-TEX-001", Severity.ERROR,
            f"\\{name} is unknown to the converter; Pandoc drops it together with its arguments, so text is lost{suffix}",
            entry.prose_location or where,
        )
    return Finding(
        "L2W-I-TEX-001", Severity.INFO,
        f"\\{name} is unknown to the converter and is ignored (no text in its arguments){suffix}", where,
    )


__all__ = ["ProbeResult", "collect_usage", "defined_names", "mask_math", "probe"]
