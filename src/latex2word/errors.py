"""Exit codes and the exception hierarchy shared by all modules."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum


class ExitCode(IntEnum):
    """Process exit codes of the ``latex2word`` command."""

    OK = 0
    TOOLS_UNAVAILABLE = 1
    ERROR = 2
    PROBLEMS = 1  # alias of 1: the project or its dependencies have problems (M2 inspect/figures)


class Latex2WordError(Exception):
    """Base class for expected, user-facing errors (no stack trace shown)."""

    exit_code: ExitCode = ExitCode.ERROR


@dataclass(frozen=True)
class ConfigIssue:
    """One problem found in a configuration file."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


class ConfigError(Latex2WordError):
    """The configuration file is unreadable, malformed or semantically invalid."""

    def __init__(self, message: str, issues: Sequence[ConfigIssue] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.issues: tuple[ConfigIssue, ...] = tuple(issues) or (ConfigIssue("", message),)


class WorkspaceError(Latex2WordError):
    """A workspace operation failed."""


class WorkspaceSafetyError(WorkspaceError):
    """A workspace operation was refused because it would be unsafe."""


class StagingError(Latex2WordError):
    """Staging was refused or its verification failed."""


class ProfileError(ConfigError):
    """A conversion profile is missing, malformed, incomplete or inconsistent."""


class BuildError(Latex2WordError):
    """A build step failed in a way that is not a document diagnostic."""
