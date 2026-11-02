"""Helpers that build tiny LaTeX projects, configs and fake tool environments in tmp dirs."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import yaml

from latex2word.process import CommandResult

# Minimal valid SVG used by fake renderers.
GOOD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="10pt" height="5pt" viewBox="0 0 10 5">'
    '<path d="M0 0L10 5"/></svg>'
)
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF_BYTES = b"%PDF-1.4\n" + b"\x00\x01\x02" * 8 + b"\n%%EOF\n"


def make_project(base: Path, files: Mapping[str, str | bytes], *, name: str = "project") -> Path:
    """Create ``base/name`` containing ``files`` (relative path -> text or bytes); return the root."""
    root = base / name
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_bytes(content.encode("utf-8"))
        else:
            path.write_bytes(content)
    return root


def write_project_config(
    base: Path,
    root: Path,
    *,
    main: str = "main.tex",
    workspace: Path | None = None,
    extra: Mapping[str, object] | None = None,
) -> Path:
    """Write a config file next to (not inside) the project; the workspace lives outside the project."""
    data: dict[str, object] = {
        "schema_version": 1,
        "project": {"name": "fixture", "root": str(root), "main": main},
        "profile": "ieee-conference",
        "workspace": {"root": str(workspace or base / "workspace")},
        "tools": {"timeout_s": 30},
    }
    data.update(extra or {})
    path = base / "config.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def tree_digest(root: Path) -> dict[str, str]:
    """``relative path -> sha256`` of every file under ``root`` (for 'source unchanged' checks)."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def make_dir_link(link: Path, target: Path) -> bool:
    """Create a directory symlink (POSIX) or junction (Windows). Returns False if impossible."""
    try:
        if sys.platform == "win32":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
            )
            return completed.returncode == 0
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        return False


def ok_result(args: tuple[str, ...] = ("tool",), stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(args, 0, stdout, stderr, False, 0.01)


class ScriptedRunner:
    """A fake command runner: ``handler(args, cwd)`` returns a CommandResult and may write files."""

    def __init__(self, handler) -> None:  # noqa: ANN001
        self.handler = handler
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []

    def __call__(self, args, *, timeout, cwd=None, env=None, input_text=None) -> CommandResult:  # noqa: ANN001
        argv = tuple(str(a) for a in args)
        assert timeout > 0
        self.calls.append((argv, Path(cwd) if cwd else None))
        return self.handler(argv, Path(cwd) if cwd else None)
