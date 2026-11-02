"""Dependency-driven staging into a latex2word-owned workspace.

Only files named by the caller (the reachable source dependencies plus explicitly
configured extras) are copied; directories are never copied wholesale. Project-
relative paths are preserved under ``<workspace>/staging``. Files are copied
byte-for-byte, hashed with SHA-256, and every staged copy is verified against its
source afterwards. Sources are never modified.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from latex2word.errors import StagingError
from latex2word.logging_utils import log_event
from latex2word.workspace import Workspace, is_link_like, portable_name_problem, verify_owned

_log = logging.getLogger(__name__)

STAGING_DIRNAME = "staging"
STAGING_SCHEMA = "latex2word.staging/1"
STAGING_MANIFEST_NAME = "staging_manifest.json"


@dataclass(frozen=True)
class StagedFile:
    source: str  # original project-relative POSIX path
    staged: str  # staged path relative to the staging directory (same as source)
    sha256: str
    size: int
    dependency_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "staged": self.staged,
            "sha256": self.sha256,
            "size": self.size,
            "dependency_type": self.dependency_type,
        }


@dataclass(frozen=True)
class StagingManifest:
    files: tuple[StagedFile, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"schema": STAGING_SCHEMA, "schema_version": 1, "files": [f.to_dict() for f in self.files]}

    def by_source(self, source: str) -> StagedFile | None:
        return next((f for f in self.files if f.source == source), None)


def staging_dir(workspace: Workspace) -> Path:
    return workspace.path / STAGING_DIRNAME


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_relative(rel: str) -> PurePosixPath:
    """Validate a project-relative path under Windows *and* POSIX rules; the verdict never depends on the host."""
    pure = PurePosixPath(rel.replace("\\", "/"))
    if pure.is_absolute() or pure.anchor or (len(rel) > 1 and rel[1] == ":"):
        raise StagingError(f"refusing absolute path: {rel}")
    if not pure.parts or any(part in ("", "..") for part in pure.parts):
        raise StagingError(f"refusing path that escapes the project root: {rel}")
    for part in pure.parts:
        problem = portable_name_problem(part)
        if problem is not None:
            raise StagingError(f"refusing path {rel!r}: segment {part!r} {problem}")
    return pure


def _source_path(root: Path, rel: PurePosixPath) -> Path:
    """The source file for ``rel``; refuses anything that is not a regular file inside the project."""
    root_real = Path(os.path.realpath(root))
    source = Path(os.path.normpath(root / Path(*rel.parts)))
    if not source.is_relative_to(Path(os.path.normpath(root))):
        raise StagingError(f"refusing path that escapes the project root: {rel}")
    if not source.is_file():
        raise StagingError(f"source file does not exist: {rel}")
    if not Path(os.path.realpath(source)).is_relative_to(root_real):
        raise StagingError(f"refusing symlink or junction that leaves the project root: {rel}")
    return source


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _reset_staging(workspace: Workspace) -> Path:
    verify_owned(workspace.path)
    target = staging_dir(workspace)
    if is_link_like(target):
        raise StagingError(f"staging directory is a symlink or junction: {target}")
    if target.exists():
        if not target.is_dir():
            raise StagingError(f"staging path is not a directory: {target}")
        shutil.rmtree(target)
    target.mkdir()
    return target


def stage_files(
    root: Path,
    entries: Sequence[tuple[str, str]],
    workspace: Workspace,
) -> StagingManifest:
    """Stage ``entries`` (``(project-relative path, dependency type)``) into ``workspace``.

    ``workspace`` must be a verified latex2word-owned workspace (ownership is
    re-checked here). Any previous staging directory is replaced. Raises
    :class:`StagingError` for unsafe paths or a failed verification; on failure
    the partially written staging directory is left for inspection.
    """
    root = Path(root)
    target_root = _reset_staging(workspace)
    staged: list[StagedFile] = []
    seen: dict[str, tuple[str, Path]] = {}  # case-folded path -> (first spelling, its source)
    for rel_text, dependency_type in entries:
        rel = _check_relative(rel_text)
        key = rel.as_posix()
        source = _source_path(root, rel)
        earlier = seen.get(key.casefold())
        if earlier is not None:
            first_key, first_source = earlier
            if key == first_key:
                continue
            # Same file under two spellings (a case-insensitive source file system) is one
            # staged copy; two different files would overwrite each other on Windows/macOS.
            if not _same_file(first_source, source):
                raise StagingError(f"paths differ only in letter case and would collide when staged: {first_key}, {key}")
            staged.append(StagedFile(key, first_key, _sha256_of(source), source.stat().st_size, dependency_type))
            continue
        seen[key.casefold()] = (key, source)
        destination = Path(os.path.normpath(target_root / Path(*rel.parts)))
        if not destination.is_relative_to(target_root):
            raise StagingError(f"refusing destination outside the staging directory: {rel}")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        except OSError as exc:
            raise StagingError(f"cannot stage {key}: {exc}") from exc
        staged.append(StagedFile(key, key, _sha256_of(source), source.stat().st_size, dependency_type))
    manifest = StagingManifest(tuple(staged))
    verify_staging(root, manifest, workspace)
    log_event(_log, logging.INFO, "staging complete", files=len(staged))
    return manifest


def verify_staging(root: Path, manifest: StagingManifest, workspace: Workspace) -> None:
    """Raise :class:`StagingError` unless every staged copy and its source match the recorded hash."""
    verify_owned(workspace.path)
    target_root = staging_dir(workspace)
    for entry in manifest.files:
        source = Path(root) / Path(*PurePosixPath(entry.source).parts)
        copy = target_root / Path(*PurePosixPath(entry.staged).parts)
        if not copy.is_file():
            raise StagingError(f"staged file missing: {entry.staged}")
        if _sha256_of(copy) != entry.sha256:
            raise StagingError(f"staged copy differs from the source hash: {entry.staged}")
        if _sha256_of(source) != entry.sha256:
            raise StagingError(f"source changed while staging: {entry.source}")


def write_staging_manifest(manifest: StagingManifest, workspace: Workspace) -> Path:
    verify_owned(workspace.path)
    path = workspace.path / STAGING_MANIFEST_NAME
    path.write_bytes((json.dumps(manifest.to_dict(), indent=2) + "\n").encode("utf-8"))
    return path
