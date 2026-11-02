"""Source rewrites applied to the *staged copy only*, and the transformation manifest.

Every change latex2word makes to staged LaTeX is an :class:`Edit` (a span of the
original staged text, its replacement and the reason). Edits are applied once per
file, never overlap, and each one becomes a :class:`Transformation` record
(source, location, original construct, replacement, reason) in the build
manifest. The user's project tree is never written.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from latex2word.errors import BuildError
from latex2word.logging_utils import log_event
from latex2word.source_graph import SourceGraph

_log = logging.getLogger(__name__)

TRANSFORMATIONS_SCHEMA = "latex2word.transformations/1"


@dataclass(frozen=True)
class Edit:
    """Replace ``text[start:end]`` of staged file ``file`` (project-relative POSIX) by ``replacement``."""

    file: str
    start: int
    end: int
    replacement: str
    kind: str  # e.g. figure-tikz, figure-asset, figure-width, bibliography-remove, bibliography-inline
    reason: str
    subject: str | None = None  # figure id, bibliography key, ...


@dataclass(frozen=True)
class Transformation:
    source: str  # staged file (== source file path, project-relative)
    line: int
    kind: str
    original: str
    replacement: str
    reason: str
    subject: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "location": {"file": self.source, "line": self.line},
            "kind": self.kind,
            "subject": self.subject,
            "original": self.original,
            "replacement": self.replacement,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FileChange:
    file: str
    sha256_before: str
    sha256_after: str


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def drop_contained(edits: Iterable[Edit]) -> list[Edit]:
    """Remove edits lying entirely inside another edit of the same file (the outer one wins)."""
    kept: list[Edit] = []
    ordered = sorted(edits, key=lambda e: (e.file, e.start, -(e.end - e.start)))
    for edit in ordered:
        if any(k.file == edit.file and k.start <= edit.start and edit.end <= k.end and k is not edit for k in kept):
            continue
        kept.append(edit)
    return kept


def apply_edits(
    graph: SourceGraph,
    staging: Path,
    edits: Iterable[Edit],
) -> tuple[list[Transformation], list[FileChange]]:
    """Apply ``edits`` to the staged files; return transformation records and per-file hashes.

    Offsets refer to the text recorded in ``graph.files`` (identical to the staged
    copy, decoded the same way). Partially overlapping edits raise :class:`BuildError`.
    Files are rewritten as UTF-8 without a byte-order mark; untouched files stay
    byte-identical to the source.
    """
    by_file: dict[str, list[Edit]] = defaultdict(list)
    for edit in drop_contained(edits):
        by_file[edit.file].append(edit)
    records: list[Transformation] = []
    changes: list[FileChange] = []
    for file in sorted(by_file):
        scanned = graph.files.get(file)
        if scanned is None:
            raise BuildError(f"cannot rewrite {file}: it was not scanned")
        text = scanned.text
        ordered = sorted(by_file[file], key=lambda e: e.start)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.start < previous.end:
                raise BuildError(
                    f"overlapping rewrites in {file} at offsets {previous.start}-{previous.end} "
                    f"({previous.kind}) and {current.start}-{current.end} ({current.kind})"
                )
        pieces: list[str] = []
        cursor = 0
        for edit in ordered:
            pieces.append(text[cursor : edit.start])
            pieces.append(edit.replacement)
            cursor = edit.end
            records.append(
                Transformation(
                    file,
                    scanned.lines.line(edit.start),
                    edit.kind,
                    text[edit.start : edit.end],
                    edit.replacement,
                    edit.reason,
                    edit.subject,
                )
            )
        pieces.append(text[cursor:])
        rewritten = "".join(pieces)
        target = staging / Path(*PurePosixPath(file).parts)
        target.write_bytes(rewritten.encode("utf-8"))
        changes.append(FileChange(file, _sha(text), _sha(rewritten)))
    records.sort(key=lambda r: (r.source, r.line, r.kind))
    log_event(_log, logging.INFO, "source rewrites applied", transformations=len(records), files=len(changes))
    return records, changes


def manifest(records: list[Transformation], changes: list[FileChange]) -> dict[str, Any]:
    return {
        "schema": TRANSFORMATIONS_SCHEMA,
        "schema_version": 1,
        "transformations": [r.to_dict() for r in records],
        "files": [
            {"file": c.file, "sha256_before": c.sha256_before, "sha256_after": c.sha256_after} for c in changes
        ],
    }


def manifest_json(records: list[Transformation], changes: list[FileChange]) -> str:
    return json.dumps(manifest(records, changes), indent=2) + "\n"
