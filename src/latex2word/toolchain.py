"""Toolchain discovery (``latex2word doctor``).

Discovery only: nothing here modifies the machine. Executables are located on
``PATH``; versions come from safe, time-limited probes, or (for filters that
cannot be probed) from installed package metadata read from disk.
"""

from __future__ import annotations

import logging
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from latex2word.diagnostics import Capability, CapabilityState, Diagnostic, DoctorReport, Role, Severity, Status
from latex2word.logging_utils import log_event
from latex2word.pandoc_compat import PandocAssessment, PandocCompat, assess_pandoc, compat_findings, parse_pandoc_version
from latex2word.pdf_svg import DEFAULT_PDF_BACKENDS
from latex2word.process import CommandResult, run_command
from platform import platform as platform_description
from platform import python_version

_log = logging.getLogger(__name__)

DEFAULT_PROBE_TIMEOUT_S = 30.0
_DETAIL_LIMIT = 300

Which = Callable[[str], str | None]
Runner = Callable[..., CommandResult]


class VersionStrategy(Enum):
    COMMAND = "command"  # run the tool with ``version_args``
    DIST_METADATA = "dist-metadata"  # read installed package metadata; never execute


@dataclass(frozen=True)
class ToolSpec:
    """Static description of one tool the doctor inspects."""

    name: str
    role: Role
    executables: tuple[str, ...]
    version_args: tuple[str, ...] = ("--version",)
    strategy: VersionStrategy = VersionStrategy.COMMAND
    dist_name: str | None = None
    windows_only: bool = False
    running_interpreter: bool = False


_POWERSHELL_ARGS = ("-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()")

TOOL_SPECS: tuple[ToolSpec, ...] = (
    # Required: the build runs these (Pandoc conversion; latex + dvisvgm for TikZ/circuitikz/pgfplots figures).
    ToolSpec("python", Role.REQUIRED, ("python",), running_interpreter=True),
    ToolSpec("pandoc", Role.REQUIRED, ("pandoc",)),
    ToolSpec("latex", Role.REQUIRED, ("latex",)),
    ToolSpec("dvisvgm", Role.REQUIRED, ("dvisvgm",)),
    # Optional / feature-dependent. pdflatex is reported as a TeX capability only: no build step runs it.
    ToolSpec("pdflatex", Role.OPTIONAL, ("pdflatex",)),
    ToolSpec("inkscape", Role.OPTIONAL, ("inkscape",)),
    ToolSpec("rsvg-convert", Role.OPTIONAL, ("rsvg-convert",)),
    ToolSpec("pdftocairo", Role.OPTIONAL, ("pdftocairo",), version_args=("-v",)),
    # A Pandoc filter: reads an AST on stdin and has no usable --version.
    ToolSpec(
        "pandoc-tex-numbering",
        Role.OPTIONAL,
        ("pandoc-tex-numbering",),
        strategy=VersionStrategy.DIST_METADATA,
        dist_name="pandoc-tex-numbering",
    ),
    # Reported on Windows only; never an architectural dependency.
    ToolSpec("powershell", Role.INFORMATIONAL, ("powershell",), _POWERSHELL_ARGS, windows_only=True),
    ToolSpec("pwsh", Role.INFORMATIONAL, ("pwsh",), _POWERSHELL_ARGS, windows_only=True),
)

# Inspected for the TeX capability section only (template discovery); not part of the readiness registry.
KPSEWHICH = ToolSpec("kpsewhich", Role.OPTIONAL, ("kpsewhich",))

_PANDOC_PROBES: tuple[tuple[str, str], ...] = (
    ("latex-reader", "LaTeX reader"),
    ("ast-api-version", "pinned pandoc-api-version"),
    ("docx-writer", "DOCX writer"),
    ("lua-filters", "Lua filters"),
    ("citeproc", "citeproc"),
)


def _degraded_status(role: Role) -> Status:
    """Status for a tool that is missing or unusable."""
    if role is Role.REQUIRED:
        return Status.FAIL
    if role is Role.OPTIONAL:
        return Status.WARN
    return Status.SKIP


def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _truncate(text: str, limit: int = _DETAIL_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _locate(spec: ToolSpec, which: Which, interpreter: str) -> str | None:
    if spec.running_interpreter:
        return interpreter
    for executable in spec.executables:
        found = which(executable)
        if found:
            return found
    return None


def _missing(spec: ToolSpec) -> Diagnostic:
    return Diagnostic.of(
        spec.name,
        _degraded_status(spec.role),
        "not found on PATH",
        role=spec.role,
    )


def _unusable(spec: ToolSpec, path: str, message: str, detail: str | None = None) -> Diagnostic:
    return Diagnostic.of(
        spec.name,
        _degraded_status(spec.role),
        message,
        role=spec.role,
        path=path,
        detail=detail,
    )


def _from_probe(spec: ToolSpec, path: str, result: CommandResult, timeout_s: float) -> Diagnostic:
    stderr_detail = _truncate(result.stderr) if result.stderr.strip() else None
    if result.error is not None:
        return _unusable(spec, path, "found but could not be executed", _truncate(result.error))
    if result.timed_out:
        return _unusable(spec, path, f"version probe timed out after {timeout_s:g}s", stderr_detail)
    if result.returncode != 0:
        return _unusable(spec, path, f"version probe exited with code {result.returncode}", stderr_detail)
    version = _first_line(result.stdout) or _first_line(result.stderr)
    if version is None:
        return Diagnostic.of(
            spec.name, Status.PASS, "present, version unavailable", role=spec.role, path=path
        )
    return Diagnostic.of(spec.name, Status.PASS, "found", role=spec.role, path=path, version=version)


def _normalise_dist_name(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def _site_packages_candidates(executable: Path) -> list[Path]:
    """Site-packages directories of the environment that owns ``executable``."""
    prefix = executable.parent.parent
    candidates = [prefix / "Lib" / "site-packages"]
    for libdir in ("lib", "lib64"):
        candidates.extend(sorted((prefix / libdir).glob("python*/site-packages")))
    return [c for c in candidates if c.is_dir()]


def _metadata_version(metadata_file: Path) -> str | None:
    try:
        with metadata_file.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    break  # end of headers
                if line.lower().startswith("version:"):
                    return line.split(":", 1)[1].strip() or None
    except OSError:
        return None
    return None


def find_dist_version(executable: str | Path, dist_name: str) -> str | None:
    """Version of ``dist_name`` installed next to ``executable``, or ``None``.

    Reads ``*.dist-info/METADATA`` from the environment owning the executable.
    Returns ``None`` (never a guess) when zero or several distributions match.
    """
    pattern = f"{_normalise_dist_name(dist_name)}-*.dist-info"
    matches: list[Path] = []
    for site in _site_packages_candidates(Path(executable).resolve()):
        matches.extend(site.glob(pattern))
    if len(matches) != 1:
        return None
    return _metadata_version(matches[0] / "METADATA")


def _from_metadata(spec: ToolSpec, path: str) -> Diagnostic:
    assert spec.dist_name is not None
    version = find_dist_version(path, spec.dist_name)
    if version is None:
        return Diagnostic.of(
            spec.name,
            Status.PASS,
            "present, version unavailable",
            role=spec.role,
            path=path,
            detail="executable not run: it reads a Pandoc AST on stdin and has no version option",
        )
    return Diagnostic.of(
        spec.name,
        Status.PASS,
        "found",
        role=spec.role,
        path=path,
        version=version,
        detail="version read from installed package metadata; executable not run",
    )


def check_tool(
    spec: ToolSpec,
    *,
    which: Which = shutil.which,
    runner: Runner = run_command,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    interpreter: str = sys.executable,
) -> Diagnostic:
    """Inspect one tool and return a :class:`Diagnostic`."""
    path = _locate(spec, which, interpreter)
    if path is None:
        return _missing(spec)
    if spec.strategy is VersionStrategy.DIST_METADATA:
        return _from_metadata(spec, path)
    result = runner((path, *spec.version_args), timeout=timeout_s)
    diagnostic = _from_probe(spec, path, result, timeout_s)
    if spec.running_interpreter and sys.prefix != sys.base_prefix:
        diagnostic = _with_detail(diagnostic, "running interpreter (virtual environment)")
    return diagnostic


def _with_detail(diagnostic: Diagnostic, detail: str) -> Diagnostic:
    merged = f"{diagnostic.detail}; {detail}" if diagnostic.detail else detail
    return replace(diagnostic, detail=merged)


def _tool_capability(group: str, name: str, diagnostics: dict[str, Diagnostic]) -> Capability:
    diagnostic = diagnostics.get(name)
    if diagnostic is None:
        return Capability(group, name, CapabilityState.UNTESTED, detail="not inspected")
    if diagnostic.status is Status.PASS:
        return Capability(group, name, CapabilityState.AVAILABLE, diagnostic.version or diagnostic.message)
    if diagnostic.path is None:
        return Capability(group, name, CapabilityState.MISSING, detail=diagnostic.message)
    return Capability(group, name, CapabilityState.INCOMPATIBLE, detail=diagnostic.message)


def _pandoc_capabilities(diagnostic: Diagnostic | None, assessment: PandocAssessment | None) -> list[Capability]:
    names = ("version", "compatibility", *(name for name, _ in _PANDOC_PROBES))
    if diagnostic is None or diagnostic.path is None:
        return [Capability("pandoc", n, CapabilityState.MISSING, detail="pandoc not found on PATH") for n in names]
    if assessment is None:  # found, but --version failed
        return [
            Capability("pandoc", n, CapabilityState.INCOMPATIBLE, detail=f"pandoc unusable: {diagnostic.message}")
            for n in names
        ]
    compat = assessment.compat
    compat_state = CapabilityState.INCOMPATIBLE if compat is PandocCompat.UNSUPPORTED else CapabilityState.AVAILABLE
    capabilities = [
        Capability("pandoc", "version", CapabilityState.AVAILABLE, assessment.version),
        Capability("pandoc", "compatibility", compat_state, compat.value, "; ".join(assessment.reasons) or None),
    ]
    results = {c.name: c for c in assessment.checks}
    for name, _ in _PANDOC_PROBES:
        check = results.get(name)
        if check is None:
            why = "; ".join(assessment.reasons) if assessment.reasons else "probe not run (--no-probe)"
            capabilities.append(Capability("pandoc", name, CapabilityState.UNTESTED, detail=why))
        else:
            state = CapabilityState.AVAILABLE if check.ok else CapabilityState.INCOMPATIBLE
            capabilities.append(Capability("pandoc", name, state, detail=check.detail))
    return capabilities


def _svg_capabilities(diagnostics: dict[str, Diagnostic]) -> list[Capability]:
    backends = [_tool_capability("svg", name, diagnostics) for name in DEFAULT_PDF_BACKENDS]
    selected = next((c for c in backends if c.state is CapabilityState.AVAILABLE), None)
    if selected is not None:
        summary = Capability("svg", "pdf-backend", CapabilityState.AVAILABLE, selected.name, "first available in preference order")
    else:
        state = (
            CapabilityState.INCOMPATIBLE
            if any(c.state is CapabilityState.INCOMPATIBLE for c in backends)
            else CapabilityState.MISSING
        )
        summary = Capability("svg", "pdf-backend", state, detail="none of " + ", ".join(DEFAULT_PDF_BACKENDS) + " is usable")
    return [summary, *backends, _tool_capability("svg", "rsvg-convert", diagnostics)]


def environment_summary(pandoc_version: str | None = None) -> dict[str, str | None]:
    """The environment a report was produced on: interpreter, OS and Pandoc versions (no paths)."""
    return {"python": python_version(), "platform": platform_description(), "pandoc": pandoc_version}


def inspect_toolchain(
    *,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    which: Which = shutil.which,
    runner: Runner = run_command,
    interpreter: str = sys.executable,
    platform: str = sys.platform,
    specs: Sequence[ToolSpec] = TOOL_SPECS,
    probe: bool = True,
) -> DoctorReport:
    """Inspect every applicable tool; all dependencies are injectable for tests.

    ``probe`` runs the Pandoc capability probe; without it the Pandoc classification rests on its
    version alone and the capability checks are reported UNTESTED. An UNSUPPORTED Pandoc makes the
    required ``pandoc`` tool FAIL, so it counts as unusable for the exit code.
    """
    diagnostics: list[Diagnostic] = []
    for spec in specs:
        if spec.windows_only and platform != "win32":
            continue
        diagnostic = check_tool(
            spec, which=which, runner=runner, timeout_s=timeout_s, interpreter=interpreter
        )
        log_event(
            _log,
            logging.DEBUG,
            "tool inspected",
            tool=spec.name,
            status=diagnostic.status.value,
            path=diagnostic.path,
        )
        diagnostics.append(diagnostic)

    assessment: PandocAssessment | None = None
    findings = []
    for index, diagnostic in enumerate(diagnostics):
        if diagnostic.subsystem != "pandoc" or diagnostic.status is not Status.PASS or diagnostic.path is None:
            continue
        assessment = assess_pandoc(
            parse_pandoc_version(diagnostic.version or ""),
            path=diagnostic.path, runner=runner, timeout_s=timeout_s, probe=probe,
        )
        findings = compat_findings(assessment)
        if assessment.compat is PandocCompat.UNSUPPORTED:
            message = "UNSUPPORTED: " + "; ".join(assessment.reasons)
            diagnostics[index] = replace(diagnostic, status=Status.FAIL, severity=Severity.ERROR, message=message)
        elif assessment.compat is PandocCompat.TESTED_UNCERTIFIED:
            diagnostics[index] = _with_detail(diagnostic, "TESTED_UNCERTIFIED")

    by_name = {d.subsystem: d for d in diagnostics}
    kpsewhich = check_tool(KPSEWHICH, which=which, runner=runner, timeout_s=timeout_s)
    capabilities = (
        *_pandoc_capabilities(by_name.get("pandoc"), assessment),
        *_svg_capabilities(by_name),
        _tool_capability("tex", "latex", by_name),
        _tool_capability("tex", "pdflatex", by_name),
        _tool_capability("tex", "kpsewhich", {"kpsewhich": kpsewhich}),
    )
    pandoc_version = assessment.version if assessment is not None else None
    return DoctorReport(
        tuple(diagnostics), capabilities, tuple(findings), environment_summary(pandoc_version)
    )
