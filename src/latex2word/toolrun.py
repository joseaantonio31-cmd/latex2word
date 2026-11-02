"""Logged external tool runs for the figure pipeline.

Wraps the M1 process runner: every run records argv, cwd, stdout, stderr, exit
code and elapsed time, and always executes with an explicit working directory
that the caller owns (never a tool's installation directory).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from latex2word.process import CommandResult, run_command

Which = Callable[[str], str | None]
Runner = Callable[..., CommandResult]

# Reproducible TeX output (no wall-clock dates in DVI/PDF where honoured).
DETERMINISTIC_ENV: Mapping[str, str] = {"SOURCE_DATE_EPOCH": "0", "FORCE_SOURCE_DATE": "1"}


@dataclass(frozen=True)
class ToolRun:
    argv: tuple[str, ...]
    cwd: str
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.returncode == 0

    def failure_reason(self) -> str:
        if self.error:
            return f"could not start: {self.error}"
        if self.timed_out:
            return f"timed out after {self.duration_s:.0f}s"
        return f"exit code {self.returncode}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration_s, 3),
            "error": self.error,
        }


def run_logged(
    args: list[str | Path],
    *,
    cwd: Path,
    timeout: float,
    runner: Runner = run_command,
    env: Mapping[str, str] | None = None,
) -> ToolRun:
    """Run ``args`` in ``cwd`` and return a :class:`ToolRun` (never raises for tool failures)."""
    merged = {**os.environ, **(env or {})}
    result = runner(args, timeout=timeout, cwd=cwd, env=merged)
    return ToolRun(
        result.args,
        str(cwd),
        result.returncode,
        result.stdout,
        result.stderr,
        result.timed_out,
        result.duration_s,
        result.error,
    )
