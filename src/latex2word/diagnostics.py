"""Structured diagnostics and their human/JSON presentation.

This module is pure data + formatting: it never probes the machine.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from latex2word import __version__
from latex2word.errors import ExitCode

if TYPE_CHECKING:  # Finding lives in source_graph, which imports this module
    from latex2word.source_graph import Finding

DOCTOR_SCHEMA = "latex2word.doctor/1"


class Status(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Role(str, Enum):
    """How much latex2word depends on a subsystem."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    INFORMATIONAL = "informational"


_SEVERITY_FOR_STATUS = {
    Status.PASS: Severity.INFO,
    Status.SKIP: Severity.INFO,
    Status.WARN: Severity.WARNING,
    Status.FAIL: Severity.ERROR,
}


@dataclass(frozen=True)
class Diagnostic:
    """One finding about a tool or subsystem."""

    subsystem: str
    status: Status
    severity: Severity
    message: str
    role: Role = Role.REQUIRED
    path: str | None = None
    version: str | None = None
    detail: str | None = None

    @classmethod
    def of(
        cls,
        subsystem: str,
        status: Status,
        message: str,
        *,
        role: Role = Role.REQUIRED,
        path: str | None = None,
        version: str | None = None,
        detail: str | None = None,
    ) -> Diagnostic:
        """Build a diagnostic whose severity is derived from ``status``."""
        return cls(subsystem, status, _SEVERITY_FOR_STATUS[status], message, role, path, version, detail)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subsystem": self.subsystem,
            "role": self.role.value,
            "status": self.status.value,
            "severity": self.severity.value,
            "message": self.message,
            "path": self.path,
            "version": self.version,
            "detail": self.detail,
        }


class CapabilityState(str, Enum):
    """Whether one capability of an external tool can be used."""

    AVAILABLE = "AVAILABLE"  # present and verified
    MISSING = "MISSING"  # the tool is not installed
    UNTESTED = "UNTESTED"  # present, but no probe was run (or could be run)
    INCOMPATIBLE = "INCOMPATIBLE"  # present, but the probe failed or the tool is unusable


@dataclass(frozen=True)
class Capability:
    """One capability line of the doctor report (``group``/``name`` is its stable key)."""

    group: str  # pandoc | svg | tex
    name: str
    state: CapabilityState
    value: str | None = None  # e.g. the version, class or selected tool
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "name": self.name,
            "state": self.state.value,
            "value": self.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DoctorReport:
    """All diagnostics of one ``doctor`` run."""

    diagnostics: tuple[Diagnostic, ...] = field(default_factory=tuple)
    capabilities: tuple[Capability, ...] = field(default_factory=tuple)
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    environment: dict[str, str | None] = field(default_factory=dict)

    def _count(self, role: Role, status: Status) -> int:
        counts = Counter((d.role, d.status) for d in self.diagnostics)
        return counts[(role, status)]

    @property
    def ready(self) -> bool:
        """True when every required tool passed."""
        return all(d.status is Status.PASS for d in self.diagnostics if d.role is Role.REQUIRED)

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.OK if self.ready else ExitCode.TOOLS_UNAVAILABLE

    def summary(self) -> dict[str, Any]:
        return {
            "status": "READY" if self.ready else "NOT_READY",
            "required_pass": self._count(Role.REQUIRED, Status.PASS),
            "required_fail": sum(
                1 for d in self.diagnostics if d.role is Role.REQUIRED and d.status is not Status.PASS
            ),
            "optional_pass": self._count(Role.OPTIONAL, Status.PASS),
            "optional_warn": self._count(Role.OPTIONAL, Status.WARN),
            "informational_pass": self._count(Role.INFORMATIONAL, Status.PASS),
            "informational_skip": self._count(Role.INFORMATIONAL, Status.SKIP),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DOCTOR_SCHEMA,
            **self.summary(),
            "latex2word_version": __version__,
            "platform": sys.platform,
            "environment": dict(self.environment),
            "tools": [d.to_dict() for d in self.diagnostics],
            "capabilities": [c.to_dict() for c in self.capabilities],
            "findings": [f.to_dict() for f in self.findings],
        }


def render_json(report: DoctorReport) -> str:
    """Stable, machine-readable rendering (ASCII-only, 2-space indent)."""
    return json.dumps(report.to_dict(), indent=2)


_SECTIONS = (
    (Role.REQUIRED, "Required"),
    (Role.OPTIONAL, "Optional"),
    (Role.INFORMATIONAL, "Informational"),
)


def _render_line(diagnostic: Diagnostic, width: int) -> str:
    text = (diagnostic.version or diagnostic.message) if diagnostic.status is Status.PASS else diagnostic.message
    if diagnostic.path:
        text += f"  ({diagnostic.path})"
    return f"  [{diagnostic.status.value}] {diagnostic.subsystem:<{width}}  {text}"


def render_result_line(report: DoctorReport) -> str:
    s = report.summary()
    return (
        f"Result: {s['status']} (required: {s['required_pass']} pass, {s['required_fail']} fail; "
        f"optional: {s['optional_pass']} pass, {s['optional_warn']} warn)"
    )


def render_text(report: DoctorReport, *, verbose: bool = False) -> str:
    """Concise human rendering; ``verbose`` adds per-tool detail lines."""
    width = max((len(d.subsystem) for d in report.diagnostics), default=0)
    lines: list[str] = []
    for role, title in _SECTIONS:
        members = [d for d in report.diagnostics if d.role is role]
        if not members:
            continue
        lines.append(title)
        for diagnostic in members:
            lines.append(_render_line(diagnostic, width))
            if verbose and diagnostic.detail:
                lines.append(f"         {diagnostic.detail}")
    if report.capabilities:
        lines.append("Capabilities")
        cap_width = max(len(f"{c.group}/{c.name}") for c in report.capabilities)
        state_width = max(len(c.state.value) for c in report.capabilities)
        for cap in report.capabilities:
            text = cap.value or ""
            if verbose and cap.detail:
                text = f"{text}  ({cap.detail})" if text else cap.detail
            lines.append(f"  [{cap.state.value:<{state_width}}] {f'{cap.group}/{cap.name}':<{cap_width}}  {text}".rstrip())
    if report.findings:
        lines.append("Findings")
        for finding in report.findings:
            lines.append(f"  {finding.severity.value.upper()}: {finding.message} [{finding.code}]")
    lines.append(render_result_line(report))
    return "\n".join(lines)
