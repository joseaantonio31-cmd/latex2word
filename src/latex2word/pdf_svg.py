"""PDF -> SVG renderer chain with fallback candidates.

Backends are tried in order; the first one that starts, exits 0 *and* produces an
SVG that passes :mod:`latex2word.svg_validate` wins. Every attempt (including
unavailable backends) is recorded.

Default order (``pdftocairo``, ``inkscape``, ``dvisvgm``) is evidence-based, see
the M2 report: on the three reference engineering plots

* ``pdftocairo`` produced vector outlines closest to a pdftocairo raster of the
  PDF, with no warnings;
* ``inkscape`` was equally close but emits "No pages selected" and "Couldn't
  parse text in PDF from UTF16" warnings (kept visible in the diagnostics);
* ``dvisvgm --pdf`` produced well-formed, "valid" SVG that rendered blank in
  rsvg-convert. XML validity alone cannot detect that, so it is the last resort.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from latex2word.process import run_command
from latex2word.svg_validate import SvgReport, validate_svg_file
from latex2word.toolrun import Runner, ToolRun, Which, run_logged

DEFAULT_PDF_BACKENDS: tuple[str, ...] = ("pdftocairo", "inkscape", "dvisvgm")


@dataclass(frozen=True)
class PdfBackend:
    name: str
    executable: str
    build_args: Callable[[str, Path, Path], list[str]]


def _pdftocairo(exe: str, pdf: Path, out: Path) -> list[str]:
    return [exe, "-svg", str(pdf), str(out)]


def _dvisvgm(exe: str, pdf: Path, out: Path) -> list[str]:
    return [exe, "--pdf", "--no-fonts", "--precision=6", "--page=1", f"--output={out}", str(pdf)]


def _inkscape(exe: str, pdf: Path, out: Path) -> list[str]:
    return [exe, str(pdf), "--export-type=svg", f"--export-filename={out}"]


BACKENDS: dict[str, PdfBackend] = {
    "pdftocairo": PdfBackend("pdftocairo", "pdftocairo", _pdftocairo),
    "dvisvgm": PdfBackend("dvisvgm", "dvisvgm", _dvisvgm),
    "inkscape": PdfBackend("inkscape", "inkscape", _inkscape),
}


@dataclass(frozen=True)
class BackendAttempt:
    backend: str
    available: bool
    run: ToolRun | None = None
    report: SvgReport | None = None
    output: Path | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.available and self.error is None and self.report is not None and self.report.valid

    def warnings(self) -> list[str]:
        """Tool stderr lines plus SVG validation warnings (never suppressed)."""
        lines: list[str] = []
        if self.run is not None:
            lines += [ln.strip() for ln in self.run.stderr.splitlines() if ln.strip()]
        if self.report is not None:
            lines += [str(i) for i in self.report.warnings]
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "available": self.available,
            "success": self.success,
            "error": self.error,
            "warnings": self.warnings(),
            "svg": self.report.to_dict() if self.report else None,
            "run": self.run.to_dict() if self.run else None,
        }


@dataclass
class PdfConversion:
    attempts: list[BackendAttempt] = field(default_factory=list)
    selected: BackendAttempt | None = None

    @property
    def ok(self) -> bool:
        return self.selected is not None


def convert_pdf_to_svg(
    pdf: Path,
    output: Path,
    workdir: Path,
    *,
    backends: Sequence[str] = DEFAULT_PDF_BACKENDS,
    timeout: float,
    which: Which = shutil.which,
    runner: Runner = run_command,
    compare_all: bool = False,
) -> PdfConversion:
    """Convert ``pdf`` to ``output`` (SVG) using the first backend that yields a valid SVG.

    Each backend writes ``<workdir>/<backend>.svg``; the winner is copied to
    ``output``. With ``compare_all`` every backend is run (for evidence) and the
    first valid one in order is still the selected result.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    conversion = PdfConversion()
    for name in backends:
        backend = BACKENDS[name]
        executable = which(backend.executable)
        if executable is None:
            conversion.attempts.append(BackendAttempt(name, False, error=f"{backend.executable} not found on PATH"))
            continue
        candidate = workdir / f"{name}.svg"
        candidate.unlink(missing_ok=True)
        run = run_logged(backend.build_args(executable, pdf, candidate), cwd=workdir, timeout=timeout, runner=runner)
        if not run.ok:
            conversion.attempts.append(BackendAttempt(name, True, run, error=run.failure_reason()))
        elif not candidate.is_file():
            conversion.attempts.append(BackendAttempt(name, True, run, error="tool exited 0 but wrote no SVG"))
        else:
            report = validate_svg_file(candidate)
            error = None if report.valid else "; ".join(i.message for i in report.errors)
            conversion.attempts.append(BackendAttempt(name, True, run, report, candidate, error))
        if conversion.selected is None and conversion.attempts[-1].success:
            conversion.selected = conversion.attempts[-1]
            if not compare_all:
                break
    if conversion.selected is not None and conversion.selected.output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(conversion.selected.output, output)
    return conversion
