"""Pandoc compatibility policy and the runtime capability probe.

latex2word is designed on the Pandoc AST observed with the versions in
:data:`CERTIFIED_VERSIONS` (``tests/test_pandoc_ast.py`` pins it,
``docs/pandoc-ast-findings.md`` explains it). The classification is about the
*installation* found on ``PATH``:

``SUPPORTED``
    a certified ``major.minor`` and, when probed, every probe check passed.
``TESTED_UNCERTIFIED``
    a version outside the certified set (newer, older but above the minimum, or
    unparseable) on which every probe check passed. The build proceeds with a
    warning; nothing guarantees the pinned AST facts beyond the probe.
``UNSUPPORTED``
    older than :data:`MINIMUM_VERSION`, or a probe check failed.

The probe only runs Pandoc on tiny inline documents written to a private
temporary directory; it never touches user files.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from latex2word.diagnostics import Severity
from latex2word.process import CommandResult
from latex2word.source_graph import Finding

Runner = Callable[..., CommandResult]

# ``major.minor`` versions on which the AST/filter tests were pinned. The single place to extend.
CERTIFIED_VERSIONS: tuple[tuple[int, int], ...] = ((3, 12),)

# Floor for the Pandoc features latex2word relies on: the filter handles the ``Figure`` block
# (``l2w_semantic.lua``; ``tests/test_pandoc_ast.py`` pins it), which exists since Pandoc 3.0
# (pandoc-api-version 1.23). Whether the Lua modules the filters use (``pandoc.json`` ...) are
# present on a given install is decided by the Lua probe, not by this coarse gate.
MINIMUM_VERSION: tuple[int, int] = (3, 0)

# ``pandoc-api-version`` series the pinned AST tests were written against (3.12 reports 1.23.1.x).
# pandoc-types follows the Haskell PVP: changes to the AST that a JSON consumer can observe (new or changed element
# types, e.g. ``Figure`` in 1.23) bump the first two components; a third component only adds API without changing
# the existing types. The pin is therefore the major series (1, 23), not a patch release. This only avoids rejecting a
# compatible AST outright: the reader probe still checks the pinned AST facts, and certification stays per Pandoc
# version (CERTIFIED_VERSIONS).
PINNED_API_VERSIONS: tuple[tuple[int, ...], ...] = ((1, 23),)

_VERSION = re.compile(r"(\d+(?:\.\d+)+)")


class PandocCompat(str, Enum):
    SUPPORTED = "SUPPORTED"
    TESTED_UNCERTIFIED = "TESTED_UNCERTIFIED"
    UNSUPPORTED = "UNSUPPORTED"  # an observed incompatibility (version floor, AST facts, failing capability)
    PROBE_ERROR = "PROBE_ERROR"  # the probe could not run (timeout, launch error, signal): no verdict on the version


@dataclass(frozen=True)
class ProbeCheck:
    """Outcome of one capability probe step."""

    name: str  # latex-reader | ast-api-version | docx-writer | lua-filters | citeproc
    ok: bool
    detail: str
    execution_error: bool = False  # the step did not run to completion (not an observation about Pandoc)


@dataclass(frozen=True)
class PandocAssessment:
    compat: PandocCompat
    version: str  # "3.12", or "unknown"
    checks: tuple[ProbeCheck, ...]  # empty when no probe ran
    reasons: tuple[str, ...]

    @property
    def probed(self) -> bool:
        return bool(self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "compat": self.compat.value,
            "version": self.version,
            "probed": self.probed,
            "reasons": list(self.reasons),
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


def parse_pandoc_version(text: str) -> str | None:
    """The first dotted version number in ``text`` (``"pandoc 3.12"`` -> ``"3.12"``)."""
    match = _VERSION.search(text)
    return match.group(1) if match else None


def _major_minor(version: str) -> tuple[int, int] | None:
    parts = version.split(".")
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
        return None
    return int(parts[0]), int(parts[1])


# --- probe documents -----------------------------------------------------------------------------

_PROBE_TEX = r"""\documentclass{article}
\usepackage{amsmath,graphicx}
\begin{document}
\section{Intro}\label{sec:probe}
See Section~\ref{sec:probe} and \cite{k1}. \Cref{sec:probe} too.
\begin{figure}\includegraphics[width=0.5\linewidth]{x.png}\caption{Cap}\label{fig:probe}\end{figure}
\begin{equation}
x = 1 \label{eq:probe}
\end{equation}
\end{document}
"""
_CITE_TEX = "\\documentclass{article}\n\\begin{document}\nAs shown in \\cite{k1}.\n\\end{document}\n"
_BIB = "@book{k1,\n  title = {Probe Title},\n  author = {Probeperson, Pat},\n  year = {2020},\n  publisher = {Probe Press}\n}\n"
_LUA = """local round = pandoc.json.decode(pandoc.json.encode({ ok = true }), false)
function Pandoc(doc)
  local text = round.ok and pandoc.utils.stringify(pandoc.Str('l2w-lua-ok')) or 'l2w-lua-bad'
  return pandoc.Pandoc(pandoc.Blocks({ pandoc.Div(pandoc.Blocks({ pandoc.Para({ pandoc.Str(text) }) }), pandoc.Attr('probe')) }), doc.meta)
end
"""
_LUA_MARKER = "l2w-lua-ok"


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        if "t" in node:
            yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _attr_dict(attr: Any) -> dict[str, str]:
    return {k: v for k, v in attr[2]}


def _ast_failures(ast: Any) -> list[str]:
    """The pinned AST facts (``tests/test_pandoc_ast.py``) that do not hold in ``ast``."""
    elements = list(_walk(ast.get("blocks", [])))
    failures: list[str] = []

    def first(tag: str) -> dict[str, Any] | None:
        return next((e for e in elements if e["t"] == tag), None)

    header = first("Header")
    if not (header and header["c"][1][0] == "sec:probe" and "number" not in _attr_dict(header["c"][1])):
        failures.append("section label as heading identifier without a number")
    if first("Figure") is None:
        failures.append("figure as a Figure block")
    image = first("Image")
    if not (image and _attr_dict(image["c"][0]).get("width") == "50%"):
        failures.append("\\linewidth width as a percentage")
    math = first("Math")
    if not (math and "\\label{eq:probe}" in math["c"][1]):
        failures.append("equation label kept inside the math string")
    # the filter detects numbered equations by this wrapper (Pandoc 3.1.3 strips it: numbers lost silently)
    if not (math and math["c"][1].lstrip().startswith("\\begin{equation}")):
        failures.append("equation environment kept inside the math string")
    # Pandoc 3.1.3 drops \Cref together with its argument
    if not any(
        e["t"] == "Link" and _attr_dict(e["c"][0]).get("reference-type") == "ref+Label" for e in elements
    ):
        failures.append("\\Cref as a link of reference type ref+Label")
    link = first("Link")
    if not (link and _attr_dict(link["c"][0]).get("reference") == "sec:probe" and link["c"][2][0] == "#sec:probe"):
        failures.append("\\ref as a link with reference attributes")
    cite = first("Cite")
    if not (
        cite
        and cite["c"][0][0].get("citationId") == "k1"
        and cite["c"][1] == [{"t": "RawInline", "c": ["latex", "\\cite{k1}"]}]
    ):
        failures.append("\\cite as a Cite node with a raw fallback")
    return failures


def _execution_failure(result: CommandResult) -> bool:
    """True when the probe step did not run to completion: launch error, timeout, or killed by a signal."""
    return result.error is not None or result.timed_out or (result.returncode is not None and result.returncode < 0)


def _check(name: str, result: CommandResult, detail: str) -> ProbeCheck:
    """A failed check for ``result``; an execution failure is marked as such, never as an incompatibility."""
    return ProbeCheck(name, False, _failed(result) if not result.ok else detail, _execution_failure(result))


def _failed(result: CommandResult) -> str:
    if result.error is not None:
        return f"could not start: {' '.join(result.error.split())[:200]}"
    if result.timed_out:
        return "timed out"
    detail = " ".join(result.stderr.split())[:200]
    return f"exit code {result.returncode}" + (f": {detail}" if detail else "")


def _reader_and_ast_checks(run: Callable[[list[str]], CommandResult], tex: Path) -> list[ProbeCheck]:
    result = run(["--from", "latex", "--to", "json", str(tex)])
    if not result.ok:
        reason, aborted = _failed(result), _execution_failure(result)
        return [
            ProbeCheck("latex-reader", False, reason, aborted),
            ProbeCheck("ast-api-version", False, "no AST available: " + reason, aborted),
        ]
    try:
        ast = json.loads(result.stdout)
        api = tuple(int(n) for n in ast["pandoc-api-version"])
    except (ValueError, KeyError, TypeError):
        return [
            ProbeCheck("latex-reader", False, "output is not a Pandoc JSON AST"),
            ProbeCheck("ast-api-version", False, "pandoc-api-version missing from the AST"),
        ]
    shown = ".".join(str(n) for n in api)
    if any(api[: len(pin)] == pin for pin in PINNED_API_VERSIONS):
        api_check = ProbeCheck("ast-api-version", True, f"pandoc-api-version {shown}")
    else:
        pinned = ", ".join(".".join(map(str, pin)) for pin in PINNED_API_VERSIONS)
        api_check = ProbeCheck("ast-api-version", False, f"pandoc-api-version {shown} is not a pinned version ({pinned})")
    failures = _ast_failures(ast)
    reader = (
        ProbeCheck("latex-reader", True, "LaTeX reader produced the pinned AST facts")
        if not failures
        else ProbeCheck("latex-reader", False, "AST differs from the pinned facts: " + "; ".join(failures))
    )
    return [reader, api_check]


def run_probe(path: str, *, runner: Runner, timeout_s: float) -> tuple[ProbeCheck, ...]:
    """Run every capability check against the Pandoc at ``path``; never raises for tool failures."""

    def run(args: list[str]) -> CommandResult:
        return runner([path, *args], timeout=timeout_s)

    with tempfile.TemporaryDirectory(prefix="l2w-probe-") as scratch:
        root = Path(scratch)
        files = {"probe.tex": _PROBE_TEX, "cite.tex": _CITE_TEX, "probe.bib": _BIB, "probe.lua": _LUA}
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8", newline="\n")
        checks = _reader_and_ast_checks(run, root / "probe.tex")

        docx = root / "probe.docx"
        result = run(["--from", "latex", "--to", "docx", "--output", str(docx), str(root / "probe.tex")])
        if result.ok and docx.is_file() and docx.read_bytes()[:2] == b"PK":
            checks.append(ProbeCheck("docx-writer", True, "wrote a DOCX package"))
        else:
            checks.append(_check("docx-writer", result, "no DOCX package written"))

        result = run(["--from", "latex", "--to", "plain", "--lua-filter", str(root / "probe.lua"), str(root / "cite.tex")])
        if result.ok and _LUA_MARKER in result.stdout:
            checks.append(ProbeCheck("lua-filters", True, "Lua filter with pandoc.json ran"))
        else:
            checks.append(_check("lua-filters", result, "the Lua filter output was not produced"))

        result = run(
            ["--from", "latex", "--to", "plain", "--citeproc", "--bibliography", str(root / "probe.bib"), str(root / "cite.tex")]
        )
        if result.ok and "Probeperson" in result.stdout:
            checks.append(ProbeCheck("citeproc", True, "formatted a citation from an inline bibliography"))
        else:
            checks.append(_check("citeproc", result, "the citation was not formatted"))
    return tuple(checks)


def assess_pandoc(
    version: str | None,
    *,
    path: str,
    runner: Runner,
    timeout_s: float,
    probe: bool,
) -> PandocAssessment:
    """Classify the Pandoc at ``path`` (``version`` as printed by ``--version``; ``None`` if unknown)."""
    shown = version or "unknown"
    parsed = _major_minor(version) if version else None
    floor = ".".join(map(str, MINIMUM_VERSION))
    if parsed is not None and parsed < MINIMUM_VERSION:
        reason = f"pandoc {shown} is older than the minimum {floor} (no Figure block / pandoc-api-version 1.23)"
        return PandocAssessment(PandocCompat.UNSUPPORTED, shown, (), (reason,))
    try:
        checks = run_probe(path, runner=runner, timeout_s=timeout_s) if probe else ()
    except OSError as exc:  # e.g. the temporary probe directory cannot be created
        return PandocAssessment(PandocCompat.PROBE_ERROR, shown, (), (f"probe setup failed: {exc}",))
    observed = [c for c in checks if not c.ok and not c.execution_error]
    if observed:  # real evidence of incompatibility wins over a step that merely did not run
        reasons = tuple(f"{c.name}: {c.detail}" for c in observed)
        return PandocAssessment(PandocCompat.UNSUPPORTED, shown, checks, reasons)
    aborted = [c for c in checks if not c.ok]
    if aborted:
        reasons = tuple(f"{c.name}: {c.detail}" for c in aborted)
        return PandocAssessment(PandocCompat.PROBE_ERROR, shown, checks, reasons)
    if parsed in CERTIFIED_VERSIONS:
        return PandocAssessment(PandocCompat.SUPPORTED, shown, checks, ())
    certified = ", ".join(".".join(map(str, v)) for v in CERTIFIED_VERSIONS)
    what = "an unrecognised version" if parsed is None else f"pandoc {shown}"
    return PandocAssessment(
        PandocCompat.TESTED_UNCERTIFIED, shown, checks, (f"{what} is not in the certified set ({certified})",)
    )


def compat_findings(assessment: PandocAssessment) -> list[Finding]:
    """One finding describing the classification (INFO / WARNING / ERROR)."""
    if assessment.compat is PandocCompat.SUPPORTED:
        return [Finding("L2W-I-PANDOC-003", Severity.INFO, f"pandoc {assessment.version} is certified (SUPPORTED)")]
    reasons = "; ".join(assessment.reasons)
    if assessment.compat is PandocCompat.TESTED_UNCERTIFIED:
        return [
            Finding(
                "L2W-W-PANDOC-003",
                Severity.WARNING,
                f"pandoc {assessment.version} is TESTED_UNCERTIFIED: {reasons}; the build continues but the AST is not certified",
            )
        ]
    if assessment.compat is PandocCompat.PROBE_ERROR:
        return [
            Finding(
                "L2W-E-PANDOC-004",
                Severity.ERROR,
                f"the Pandoc compatibility probe could not run ({reasons}); pandoc {assessment.version} was not assessed "
                "(this is a tool/probe error, not a verdict on the Pandoc version)",
            )
        ]
    return [Finding("L2W-E-PANDOC-003", Severity.ERROR, f"pandoc {assessment.version} is UNSUPPORTED: {reasons}")]
