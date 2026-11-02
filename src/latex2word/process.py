"""Run external commands safely.

Every command is an argument vector (never a shell string), runs with captured
stdout/stderr, a mandatory timeout, and no inherited stdin. Failures to start,
timeouts and non-zero exits are reported in the result rather than raised.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from latex2word.logging_utils import log_event

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one external command."""

    args: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True only for a command that started, finished in time, and exited 0."""
        return self.error is None and not self.timed_out and self.returncode == 0


def _as_text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    return data.replace("\r\n", "\n")


def run_command(
    args: Sequence[str | Path],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> CommandResult:
    """Run ``args`` without a shell and return a :class:`CommandResult`.

    ``timeout`` is mandatory (seconds, > 0). Without ``input_text`` the child's
    stdin is closed (``DEVNULL``) so that filter-style programs cannot hang
    waiting for input.

    The streams are exchanged as bytes (UTF-8, undecodable bytes replaced) so that no
    newline translation depends on the host: ``input_text`` reaches the child exactly as
    given, and CRLF in the output is reported as LF on every platform.
    """
    argv = tuple(str(arg) for arg in args)
    if not argv:
        raise ValueError("args must not be empty")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    kwargs: dict[str, Any] = {
        "capture_output": True,
        "timeout": timeout,
        "cwd": cwd,
        "env": dict(env) if env is not None else None,
        "shell": False,
        "check": False,
    }
    if input_text is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = input_text.encode("utf-8")

    started = time.monotonic()
    try:
        completed = subprocess.run(list(argv), **kwargs)
    except subprocess.TimeoutExpired as exc:
        result = CommandResult(
            argv, None, _as_text(exc.stdout), _as_text(exc.stderr), True, time.monotonic() - started
        )
    except OSError as exc:
        result = CommandResult(argv, None, "", "", False, time.monotonic() - started, error=str(exc))
    else:
        result = CommandResult(
            argv,
            completed.returncode,
            _as_text(completed.stdout),
            _as_text(completed.stderr),
            False,
            time.monotonic() - started,
        )
    log_event(
        _log,
        logging.DEBUG,
        "command finished",
        argv=" ".join(argv),
        returncode=result.returncode,
        timed_out=result.timed_out,
        error=result.error,
        duration_s=f"{result.duration_s:.2f}",
    )
    return result
