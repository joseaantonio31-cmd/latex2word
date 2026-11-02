"""Safe, ownership-marked workspaces for future staging builds.

A workspace directory is *owned* by latex2word when it carries a valid
``.latex2word-owner.json`` marker whose recorded path matches the directory.
Recursive deletion is only ever performed on an owned workspace that is not a
dangerous location (filesystem root, project root, home directory, or any
ancestor of the project root or home). No CLI command deletes paths in M1.0.
"""

from __future__ import annotations

import json
import logging
import shutil
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from latex2word.errors import WorkspaceSafetyError
from latex2word.logging_utils import log_event

_log = logging.getLogger(__name__)

APP_NAME = "latex2word"
MARKER_NAME = ".latex2word-owner.json"
MARKER_SCHEMA_VERSION = 1
DEFAULT_WORKSPACE_DIRNAME = ".latex2word"


@dataclass(frozen=True)
class OwnershipMarker:
    """Contents of ``.latex2word-owner.json``."""

    schema_version: int
    application: str
    workspace_path: str
    created_at: str
    run_id: str | None = None

    @classmethod
    def new(cls, workspace: Path, run_id: str | None = None) -> OwnershipMarker:
        return cls(
            MARKER_SCHEMA_VERSION,
            APP_NAME,
            str(workspace.resolve()),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            run_id,
        )


_WINDOWS_INVALID_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{device}{digit}" for device in ("COM", "LPT") for digit in "123456789\u00b9\u00b2\u00b3"}
)


def portable_name_problem(name: str) -> str | None:
    """Why ``name`` (one path segment) cannot be a file name on every supported OS, else ``None``.

    The rules are Windows' (the strictest) and are applied on every platform so that a
    workspace, its manifests and the staged tree mean the same thing everywhere: reserved
    device names (with or without an extension), characters Windows rejects (``:`` also
    opens NTFS alternate data streams), control characters, and a trailing dot or space
    (silently stripped by Win32, so two names would alias).
    """
    if any(ch in _WINDOWS_INVALID_CHARS or ord(ch) < 32 for ch in name):
        return "contains a character that is not allowed in Windows file names"
    if name.endswith((".", " ")):
        return "ends with a dot or a space"
    if name.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_RESERVED:
        return "is a reserved Windows device name"
    return None


def default_workspace_path(project_root: Path) -> Path:
    """``<project root>/.latex2word``."""
    return project_root / DEFAULT_WORKSPACE_DIRNAME


def _is_link_like(path: Path) -> bool:
    """Symlink, or a Windows reparse point (junction, mount point, ...)."""
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


is_link_like = _is_link_like  # public alias for other modules; M1 call sites unchanged


def write_marker(workspace: Path, marker: OwnershipMarker) -> None:
    """Write ``marker`` into ``workspace`` (UTF-8, LF)."""
    text = json.dumps(asdict(marker), indent=2) + "\n"
    (workspace / MARKER_NAME).write_bytes(text.encode("utf-8"))


def read_marker(workspace: Path) -> OwnershipMarker:
    """Parse the marker of ``workspace``; raise :class:`WorkspaceSafetyError` if unusable."""
    marker_path = workspace / MARKER_NAME
    if _is_link_like(marker_path):
        raise WorkspaceSafetyError(f"ownership marker is a link: {marker_path}")
    if not marker_path.is_file():
        raise WorkspaceSafetyError(f"not a latex2word workspace (no {MARKER_NAME}): {workspace}")
    try:
        data: Any = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceSafetyError(f"ownership marker cannot be parsed: {marker_path}: {exc}") from exc
    return _marker_from_data(data, marker_path)


def _marker_from_data(data: Any, marker_path: Path) -> OwnershipMarker:
    def bad(reason: str) -> WorkspaceSafetyError:
        return WorkspaceSafetyError(f"invalid ownership marker ({reason}): {marker_path}")

    if not isinstance(data, dict):
        raise bad("not a JSON object")
    version = data.get("schema_version")
    if isinstance(version, bool) or version != MARKER_SCHEMA_VERSION:
        raise bad(f"unsupported schema_version {version!r}")
    if data.get("application") != APP_NAME:
        raise bad(f"application is {data.get('application')!r}, expected {APP_NAME!r}")
    workspace_path = data.get("workspace_path")
    created_at = data.get("created_at")
    run_id = data.get("run_id")
    if not isinstance(workspace_path, str) or not workspace_path:
        raise bad("workspace_path missing")
    if not isinstance(created_at, str) or not created_at:
        raise bad("created_at missing")
    if run_id is not None and not isinstance(run_id, str):
        raise bad("run_id must be a string or null")
    return OwnershipMarker(MARKER_SCHEMA_VERSION, APP_NAME, workspace_path, created_at, run_id)


def verify_owned(path: Path) -> OwnershipMarker:
    """Return the marker of ``path`` if it is a genuine owned workspace, else raise."""
    if _is_link_like(path):
        raise WorkspaceSafetyError(f"workspace is a symlink or junction: {path}")
    if not path.is_dir():
        raise WorkspaceSafetyError(f"workspace directory does not exist: {path}")
    marker = read_marker(path)
    if Path(marker.workspace_path).resolve() != path.resolve():
        raise WorkspaceSafetyError(
            f"ownership marker belongs to a different directory ({marker.workspace_path}); "
            f"refusing to treat {path} as owned"
        )
    return marker


def is_owned_workspace(path: Path) -> bool:
    """True if ``path`` is a directory owned by latex2word (see :func:`verify_owned`)."""
    try:
        verify_owned(path)
    except WorkspaceSafetyError:
        return False
    return True


def assert_safe_target(path: Path, project_root: Path, *, home: Path | None = None) -> None:
    """Raise :class:`WorkspaceSafetyError` if ``path`` must never be a workspace/deletion target."""
    target = path.resolve()
    project = project_root.resolve()
    user_home = (home if home is not None else Path.home()).resolve()
    if target.parent == target:
        raise WorkspaceSafetyError(f"refusing to operate on the filesystem root: {target}")
    if target == project:
        raise WorkspaceSafetyError(f"refusing to operate on the project root: {target}")
    if target == user_home:
        raise WorkspaceSafetyError(f"refusing to operate on the home directory: {target}")
    if project.is_relative_to(target):
        raise WorkspaceSafetyError(f"refusing to operate on a parent of the project root: {target}")
    if user_home.is_relative_to(target):
        raise WorkspaceSafetyError(f"refusing to operate on a parent of the home directory: {target}")


@dataclass(frozen=True)
class Workspace:
    """A latex2word-owned directory for a project's staging builds."""

    path: Path
    project_root: Path

    @classmethod
    def create(
        cls,
        path: Path,
        project_root: Path,
        *,
        run_id: str | None = None,
        home: Path | None = None,
    ) -> Workspace:
        """Create (or reuse) an owned workspace at ``path``.

        An existing owned workspace is reused unchanged. An existing empty
        directory is adopted. Anything else that already exists is refused.
        """
        path = Path(path)
        problem = portable_name_problem(path.name) if path.name else None
        if problem is not None:
            raise WorkspaceSafetyError(f"workspace directory name {path.name!r} {problem}")
        if _is_link_like(path):
            raise WorkspaceSafetyError(f"workspace path is a symlink or junction: {path}")
        assert_safe_target(path, project_root, home=home)
        if path.exists():
            if not path.is_dir():
                raise WorkspaceSafetyError(f"workspace path exists and is not a directory: {path}")
            if is_owned_workspace(path):
                return cls(path.resolve(), project_root.resolve())
            if any(path.iterdir()):
                raise WorkspaceSafetyError(
                    f"directory exists, is not empty and is not a latex2word workspace: {path}"
                )
        else:
            path.mkdir(parents=True)
        write_marker(path, OwnershipMarker.new(path, run_id))
        log_event(_log, logging.INFO, "workspace created", path=path)
        return cls(path.resolve(), project_root.resolve())

    @classmethod
    def open(cls, path: Path, project_root: Path) -> Workspace:
        """Open an existing workspace; raises unless it is a verified owned one."""
        verify_owned(Path(path))
        return cls(Path(path).resolve(), project_root.resolve())

    @property
    def marker(self) -> OwnershipMarker:
        return verify_owned(self.path)

    def clean(self, *, home: Path | None = None) -> None:
        """Recursively delete this workspace, after every safety check passes."""
        assert_safe_target(self.path, self.project_root, home=home)
        verify_owned(self.path)
        shutil.rmtree(self.path)
        log_event(_log, logging.INFO, "workspace removed", path=self.path)
