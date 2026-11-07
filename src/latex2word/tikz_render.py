"""TikZ/circuitikz -> SVG: ``latex`` (DVI) then ``dvisvgm``.

Every side-effect file (``.aux``, ``.log``, ``.dvi``, dvisvgm temporaries) goes to
a latex2word-owned render directory. Both tools run in the same directory (the
staged main-file directory when given), so graphics that the DVI references by
relative path resolve for dvisvgm as they did for LaTeX; a graphic that dvisvgm
reports as missing or unprocessed is an error, not a partial SVG. dvisvgm options come
from an earlier validated pipeline: ``--no-fonts`` (glyphs become paths, no font
dependency), ``--exact-bbox --bbox=min`` (tight bounding box) and
``--precision=6``.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from latex2word.process import run_command
from latex2word.svg_validate import SvgReport, validate_svg_file
from latex2word.toolrun import DETERMINISTIC_ENV, Runner, ToolRun, Which, run_logged

LATEX_FLAGS = ("-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-no-shell-escape")
DVISVGM_FLAGS = ("--no-fonts", "--exact-bbox", "--bbox=min", "--precision=6")


# dvisvgm warnings that mean a graphic referenced by the DVI (psfile/pdffile/img specials) was not rendered
_LOST_RESOURCE = re.compile(
    r"not found|can't|cannot|could ?n[o']t|failed|unable|not available|disabled|not supported|unsupported",
    re.IGNORECASE,
)


def unprocessed_resources(stderr: str | None) -> list[str]:
    """dvisvgm ``WARNING:`` lines reporting a referenced graphic that is missing or was not processed."""
    lines = (line.strip() for line in (stderr or "").splitlines())
    return [line for line in lines if line.upper().startswith("WARNING") and _LOST_RESOURCE.search(line)]


def _relative_to(path: Path, base: Path) -> str:
    """``path`` relative to ``base`` with forward slashes (TeX-safe); absolute when on another drive."""
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:
        return path.resolve().as_posix()


@dataclass
class TikzRenderResult:
    figure_id: str
    ok: bool = False
    output: Path | None = None
    tex_path: Path | None = None
    runs: list[ToolRun] = field(default_factory=list)
    report: SvgReport | None = None
    error: str | None = None

    def warnings(self) -> list[str]:
        lines: list[str] = []
        if self.report is not None:
            lines += [str(i) for i in self.report.warnings]
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "figure": self.figure_id,
            "ok": self.ok,
            "error": self.error,
            "runs": [r.to_dict() for r in self.runs],
            "svg": self.report.to_dict() if self.report else None,
        }


def render_tikz(
    figure_id: str,
    document: str,
    workdir: Path,
    output: Path,
    *,
    timeout: float,
    which: Which = shutil.which,
    runner: Runner = run_command,
    source_dir: Path | None = None,
) -> TikzRenderResult:
    """Render the standalone LaTeX ``document`` to the SVG ``output``.

    ``source_dir`` is the staged directory of the project's main file. When
    given, ``latex`` runs there (as in the project's own build, so relative
    paths such as pgfplots data files resolve) and writes every side-effect
    file into ``workdir`` through ``-output-directory``.
    """
    result = TikzRenderResult(figure_id)
    latex = which("latex")
    dvisvgm = which("dvisvgm")
    if latex is None:
        result.error = "latex not found on PATH"
        return result
    if dvisvgm is None:
        result.error = "dvisvgm not found on PATH"
        return result

    workdir.mkdir(parents=True, exist_ok=True)
    tex_path = workdir / f"{figure_id}.tex"
    dvi_path = workdir / f"{figure_id}.dvi"
    dvi_path.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    tex_path.write_bytes(document.replace("\r\n", "\n").encode("utf-8"))
    result.tex_path = tex_path

    if source_dir is not None and source_dir.is_dir():
        out_dir, tex_arg = _relative_to(workdir, source_dir), _relative_to(tex_path, source_dir)
        argv = [latex, *LATEX_FLAGS, f"-output-directory={out_dir}", tex_arg]
        latex_cwd = source_dir
        dvi_arg = _relative_to(dvi_path, source_dir)
    else:
        argv, latex_cwd = [latex, *LATEX_FLAGS, tex_path.name], workdir
        dvi_arg = dvi_path.name
    latex_run = run_logged(argv, cwd=latex_cwd, timeout=timeout, runner=runner, env=DETERMINISTIC_ENV)
    result.runs.append(latex_run)
    if not latex_run.ok or not dvi_path.is_file():
        result.error = f"latex failed ({latex_run.failure_reason()}); see {figure_id}.log in the render directory"
        return result

    # The DVI refers to external graphics (\includegraphics inside a drawing) by the paths LaTeX resolved
    # from its own working directory, so dvisvgm must run in that same directory; its temporary files go to
    # the render directory, so nothing is written next to the staged sources.
    output.parent.mkdir(parents=True, exist_ok=True)
    svg_run = run_logged(
        [dvisvgm, *DVISVGM_FLAGS, f"--tmpdir={_relative_to(workdir, latex_cwd)}", f"--output={output}", dvi_arg],
        cwd=latex_cwd,
        timeout=timeout,
        runner=runner,
        env=DETERMINISTIC_ENV,
    )
    result.runs.append(svg_run)
    if not svg_run.ok or not output.is_file():
        result.error = f"dvisvgm failed ({svg_run.failure_reason()})"
        return result
    lost = unprocessed_resources(svg_run.stderr)
    if lost:
        # dvisvgm only warns when a graphic referenced by the DVI is missing or cannot be processed, and
        # still writes the rest of the drawing: the SVG would silently lack that content
        result.error = "dvisvgm could not process a graphic referenced by the drawing: " + "; ".join(lost)
        return result

    result.report = validate_svg_file(output)
    if not result.report.valid:
        result.error = "invalid SVG: " + "; ".join(i.message for i in result.report.errors)
        return result
    result.ok = True
    return result
