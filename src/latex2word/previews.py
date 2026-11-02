"""PNG previews of SVGs via ``rsvg-convert`` (inspection artifacts only).

Previews are never a primary figure format and never fail a figure run.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from latex2word.process import run_command
from latex2word.toolrun import Runner, ToolRun, Which, run_logged

PREVIEW_DPI = 150


@dataclass(frozen=True)
class PreviewResult:
    ok: bool
    output: Path | None
    message: str
    run: ToolRun | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "run": self.run.to_dict() if self.run else None}


def render_preview(
    svg: Path,
    png: Path,
    workdir: Path,
    *,
    timeout: float,
    which: Which = shutil.which,
    runner: Runner = run_command,
) -> PreviewResult:
    """Render ``svg`` to ``png``; failures are returned, not raised."""
    exe = which("rsvg-convert")
    if exe is None:
        return PreviewResult(False, None, "rsvg-convert not found on PATH; preview skipped")
    workdir.mkdir(parents=True, exist_ok=True)
    png.parent.mkdir(parents=True, exist_ok=True)
    png.unlink(missing_ok=True)
    run = run_logged(
        [exe, "-f", "png", "-b", "white", "--dpi-x", str(PREVIEW_DPI), "--dpi-y", str(PREVIEW_DPI), "-o", str(png), str(svg)],
        cwd=workdir,
        timeout=timeout,
        runner=runner,
    )
    if not run.ok or not png.is_file():
        return PreviewResult(False, None, f"rsvg-convert failed ({run.failure_reason()})", run)
    return PreviewResult(True, png, "preview written", run)
