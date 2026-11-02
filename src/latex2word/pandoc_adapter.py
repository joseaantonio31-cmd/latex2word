"""Pandoc adapter: one place that knows how latex2word invokes Pandoc.

* the executable and its version come from the M1 toolchain layer;
* the command is an argument vector (never a shell string) run with an explicit,
  caller-owned working directory and a timeout; stdout/stderr are captured and
  kept, never suppressed;
* every ``[WARNING]`` Pandoc prints is parsed; those that mean semantic loss
  become latex2word diagnostics with stable codes.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from latex2word.diagnostics import Severity, Status
from latex2word.errors import BuildError
from latex2word.logging_utils import log_event
from latex2word.process import run_command
from latex2word.source_graph import Finding
from latex2word.toolchain import TOOL_SPECS, check_tool
from latex2word.toolrun import DETERMINISTIC_ENV, Runner, ToolRun, Which, run_logged

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 120.0
_PANDOC_SPEC = next(spec for spec in TOOL_SPECS if spec.name == "pandoc")
_VERSION = re.compile(r"(\d+(?:\.\d+)+)")


@dataclass(frozen=True)
class PandocTool:
    path: str
    version: str  # e.g. "3.12"
    version_line: str


def locate_pandoc(*, which: Which = shutil.which, runner: Runner = run_command, timeout_s: float = 30.0) -> PandocTool:
    """Resolve Pandoc through the toolchain layer; raises :class:`BuildError` when unusable."""
    diagnostic = check_tool(_PANDOC_SPEC, which=which, runner=runner, timeout_s=timeout_s)
    if diagnostic.status is not Status.PASS or not diagnostic.path:
        raise BuildError(f"pandoc is required but not usable: {diagnostic.message}")
    line = diagnostic.version or ""
    match = _VERSION.search(line)
    return PandocTool(diagnostic.path, match.group(1) if match else "unknown", line)


@dataclass(frozen=True)
class PandocOptions:
    inputs: Sequence[str]  # relative to the working directory (or absolute)
    output: Path
    from_format: str = "latex"
    to_format: str = "docx"
    standalone: bool = True
    lua_filters: Sequence[Path] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
    citeproc: bool = False
    bibliography: Sequence[Path] = ()
    csl: Path | None = None
    reference_doc: Path | None = None
    resource_paths: Sequence[Path] = ()
    extra_args: Sequence[str] = ()


def build_argv(tool_path: str, options: PandocOptions) -> list[str]:
    """The Pandoc argument vector. Filters run in order; ``--citeproc`` comes after them."""
    argv: list[str] = [tool_path, "--from", options.from_format, "--to", options.to_format]
    if options.standalone:
        argv.append("--standalone")
    if options.resource_paths:
        argv += ["--resource-path", os.pathsep.join(str(p) for p in options.resource_paths)]
    for filter_path in options.lua_filters:
        argv += ["--lua-filter", str(filter_path)]
    for key, value in options.metadata.items():
        argv += ["--metadata", f"{key}={value}"]
    if options.citeproc:
        argv.append("--citeproc")
    for bibliography in options.bibliography:
        argv += ["--bibliography", str(bibliography)]
    if options.csl is not None:
        argv += ["--csl", str(options.csl)]
    if options.reference_doc is not None:
        argv += ["--reference-doc", str(options.reference_doc)]
    argv += list(options.extra_args)
    argv += ["--output", str(options.output)]
    argv += list(options.inputs)
    return argv


# (pattern, code, severity, summary) -- first match wins; patterns see the whole warning text.
_CLASSIFIERS: tuple[tuple[re.Pattern[str], str, Severity, str], ...] = (
    (re.compile(r"Could not convert TeX math", re.I), "L2W-E-MATH-002", Severity.ERROR,
     "a formula could not be converted and would appear as raw TeX"),
    (re.compile(r"Citeproc:\s*citation\s+(\S+)\s+not found", re.I), "L2W-E-CITE-001", Severity.ERROR,
     "citation key not found in the bibliography"),
    (re.compile(r"Could not fetch resource|Could not find image|File .* not found|Cannot find image", re.I),
     "L2W-E-FIG-007", Severity.ERROR, "an image could not be found or read"),
    (re.compile(r"Duplicate identifier", re.I), "L2W-E-LABEL-001", Severity.ERROR, "duplicate identifier"),
    (re.compile(r"Could not determine image size", re.I), "L2W-W-FIG-005", Severity.WARNING,
     "image size could not be determined"),
    (re.compile(r"Citeproc:", re.I), "L2W-W-CITE-002", Severity.WARNING, "citeproc warning"),
    (re.compile(r"Skipped|Unsupported|not supported|ignored", re.I), "L2W-W-PANDOC-002", Severity.WARNING,
     "content skipped by Pandoc"),
)


@dataclass(frozen=True)
class PandocWarning:
    text: str  # the full warning text without the "[WARNING]" tag
    code: str
    severity: Severity

    def to_finding(self) -> Finding:
        lines = self.text.splitlines()
        extra = f" (+{len(lines) - 1} detail line(s) in pandoc_run.json)" if len(lines) > 1 else ""
        return Finding(self.code, self.severity, f"pandoc: {lines[0] if lines else ''}{extra}")

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "code": self.code, "severity": self.severity.value}


def parse_warnings(stderr: str) -> list[PandocWarning]:
    """Split ``stderr`` into ``[WARNING]`` records (indented lines continue the record)."""
    records: list[list[str]] = []
    for line in stderr.splitlines():
        if line.startswith("[WARNING]"):
            records.append([line[len("[WARNING]"):].strip()])
        elif line.startswith("[") and "]" in line.split(" ", 1)[0]:
            records.append([])  # another tagged level (INFO/ERROR): not a warning record
        elif records and records[-1] and (line.startswith((" ", "\t")) or line == ""):
            records[-1].append(line.rstrip())
    warnings: list[PandocWarning] = []
    for lines in records:
        if not lines:
            continue
        text = "\n".join(lines).rstrip()
        code, severity = "L2W-W-PANDOC-001", Severity.WARNING
        for pattern, pcode, pseverity, _ in _CLASSIFIERS:
            if pattern.search(text):
                code, severity = pcode, pseverity
                break
        warnings.append(PandocWarning(text, code, severity))
    return warnings


@dataclass(frozen=True)
class PandocRun:
    tool: PandocTool
    run: ToolRun
    warnings: tuple[PandocWarning, ...]

    @property
    def ok(self) -> bool:
        return self.run.ok

    def failure_finding(self) -> Finding | None:
        if self.run.ok:
            return None
        return Finding(
            "L2W-E-PANDOC-001", Severity.ERROR,
            f"pandoc failed ({self.run.failure_reason()}): " + " ".join(self.run.stderr.split())[:400],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pandoc_version": self.tool.version,
            "pandoc_path": self.tool.path,
            "argv": list(self.run.argv),
            "cwd": self.run.cwd,
            "exit_code": self.run.returncode,
            "elapsed_s": round(self.run.duration_s, 3),
            "timed_out": self.run.timed_out,
            "warnings": [w.to_dict() for w in self.warnings],
            "stderr": self.run.stderr,
        }


def run_pandoc(
    tool: PandocTool,
    options: PandocOptions,
    *,
    cwd: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    runner: Runner = run_command,
) -> PandocRun:
    """Run Pandoc in ``cwd`` (which the caller must own) and return the structured result."""
    argv = build_argv(tool.path, options)
    result = run_logged(list(argv), cwd=cwd, timeout=timeout_s, runner=runner, env=DETERMINISTIC_ENV)
    warnings = tuple(parse_warnings(result.stderr))
    log_event(
        _log, logging.INFO, "pandoc finished", exit_code=result.returncode,
        elapsed_s=f"{result.duration_s:.2f}", warnings=len(warnings),
    )
    return PandocRun(tool, result, warnings)
