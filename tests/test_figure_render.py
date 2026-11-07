from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import fake_which
from projectkit import GOOD_SVG, PDF_BYTES, ScriptedRunner, ok_result

from latex2word.pdf_svg import DEFAULT_PDF_BACKENDS, convert_pdf_to_svg
from latex2word.previews import render_preview
from latex2word.process import CommandResult, run_command
from latex2word.tikz_render import render_tikz
from latex2word.toolrun import run_logged

WHICH = fake_which({"latex", "dvisvgm", "pdftocairo", "inkscape", "rsvg-convert"})


def failed(args: tuple[str, ...], code: int = 1, stderr: str = "boom") -> CommandResult:
    return CommandResult(args, code, "", stderr, False, 0.01)


def timed_out(args: tuple[str, ...]) -> CommandResult:
    return CommandResult(args, None, "", "", True, 5.0)


def write_option(argv: tuple[str, ...], prefix: str, content: str) -> None:
    for arg in argv:
        if arg.startswith(prefix):
            Path(arg[len(prefix) :]).write_text(content, encoding="utf-8")


# ---- process layer (real subprocesses, python as the child) ------------------------------------


def test_run_logged_captures_argv_cwd_streams_exit_code_and_time(tmp_path: Path):
    code = "import os,sys; print(os.getcwd()); print('err', file=sys.stderr); sys.exit(3)"
    run = run_logged([__import__("sys").executable, "-c", code], cwd=tmp_path, timeout=30)
    assert run.returncode == 3 and not run.ok
    assert Path(run.stdout.strip()).resolve() == tmp_path.resolve()
    assert "err" in run.stderr and run.cwd == str(tmp_path) and run.duration_s >= 0
    assert run.argv[1] == "-c" and run.failure_reason() == "exit code 3"


def test_real_timeout_and_missing_executable_are_reported_not_raised(tmp_path: Path):
    slow = run_logged([__import__("sys").executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, timeout=0.5)
    assert slow.timed_out and not slow.ok and "timed out" in slow.failure_reason()
    missing = run_logged(["definitely-not-a-real-tool-xyz"], cwd=tmp_path, timeout=5)
    assert missing.error and not missing.ok and "could not start" in missing.failure_reason()
    assert run_command is not None


# ---- TikZ renderer ----------------------------------------------------------------------------------


def tikz_handler(*, latex_ok: bool = True, svg: str | None = GOOD_SVG, dvisvgm_result: str = "ok"):
    def handler(argv: tuple[str, ...], cwd: Path | None) -> CommandResult:
        assert cwd is not None and cwd.is_dir()
        if argv[0] == "/fake/bin/latex":
            if not latex_ok:
                return failed(argv, 1, "! Emergency stop.")
            (cwd / argv[-1]).with_suffix(".dvi").write_bytes(b"dvi")
            return ok_result(argv)
        if dvisvgm_result == "timeout":
            return timed_out(argv)
        if dvisvgm_result == "fail":
            return failed(argv)
        if svg is not None:
            write_option(argv, "--output=", svg)
        return ok_result(argv)

    return handler


def render(tmp_path: Path, runner: ScriptedRunner, which=WHICH):
    return render_tikz(
        "fig", "\\documentclass{standalone}", tmp_path / "work", tmp_path / "out" / "fig.svg",
        timeout=10, which=which, runner=runner,
    )


def test_tikz_success_runs_latex_then_dvisvgm_with_baseline_options(tmp_path: Path):
    runner = ScriptedRunner(tikz_handler())
    result = render(tmp_path, runner)
    assert result.ok and result.report.valid
    (latex_argv, latex_cwd), (svg_argv, svg_cwd) = runner.calls
    assert latex_cwd == svg_cwd == tmp_path / "work"
    assert {"-interaction=nonstopmode", "-halt-on-error"} <= set(latex_argv)
    assert {"--no-fonts", "--exact-bbox", "--bbox=min", "--precision=6"} <= set(svg_argv)
    assert (tmp_path / "work" / "fig.tex").read_text(encoding="utf-8").startswith("\\documentclass")
    assert (tmp_path / "out" / "fig.svg").is_file()
    assert [r.cwd for r in result.runs] == [str(tmp_path / "work")] * 2


def test_tikz_missing_executables(tmp_path: Path):
    runner = ScriptedRunner(tikz_handler())
    assert "latex not found" in render(tmp_path, runner, fake_which({"dvisvgm"})).error
    assert "dvisvgm not found" in render(tmp_path, runner, fake_which({"latex"})).error
    assert runner.calls == []


def test_tikz_latex_nonzero_exit(tmp_path: Path):
    result = render(tmp_path, ScriptedRunner(tikz_handler(latex_ok=False)))
    assert not result.ok and "latex failed" in result.error and len(result.runs) == 1


def test_tikz_dvisvgm_timeout_and_failure(tmp_path: Path):
    assert "timed out" in render(tmp_path, ScriptedRunner(tikz_handler(dvisvgm_result="timeout"))).error
    assert "exit code 1" in render(tmp_path, ScriptedRunner(tikz_handler(dvisvgm_result="fail"))).error


@pytest.mark.parametrize(
    ("svg", "fragment"),
    [
        ("<svg xmlns='http://www.w3.org/2000/svg'><path", "not well-formed"),
        (None, "dvisvgm failed"),
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><g/></svg>', "no drawable geometry"),
    ],
)
def test_tikz_malformed_or_missing_renderer_output_fails(tmp_path: Path, svg: str | None, fragment: str):
    result = render(tmp_path, ScriptedRunner(tikz_handler(svg=svg)))
    assert not result.ok and fragment in result.error


@pytest.mark.parametrize(
    "warning",
    [
        "WARNING: file './img/board.pdf' not found",
        "WARNING: PDF support not available (Ghostscript not found)",
        "WARNING: can't process image file 'img/board.png'",
    ],
)
def test_tikz_graphic_that_dvisvgm_could_not_render_is_an_error_not_a_partial_svg(tmp_path: Path, warning: str):
    base = tikz_handler()

    def handler(argv: tuple[str, ...], cwd: Path | None) -> CommandResult:
        result = base(argv, cwd)
        if Path(argv[0]).name == "dvisvgm":  # writes a valid SVG of the rest of the drawing, exit code 0
            return ok_result(argv, stderr=f"processing page 1\n  {warning}\n  output written\n")
        return result

    result = render(tmp_path, ScriptedRunner(handler))
    assert not result.ok and warning in result.error and "could not process a graphic" in result.error


def test_unrelated_dvisvgm_messages_do_not_fail_a_render(tmp_path: Path):
    base = tikz_handler()

    def handler(argv: tuple[str, ...], cwd: Path | None) -> CommandResult:
        result = base(argv, cwd)
        if Path(argv[0]).name == "dvisvgm":
            return ok_result(argv, stderr="pre-processing DVI file (format version 2)\nprocessing page 1\n")
        return result

    assert render(tmp_path, ScriptedRunner(handler)).ok


# ---- real toolchain: a drawing that embeds an external PDF ------------------------------------------


def _pdf_with_a_filled_square(rgb: str) -> bytes:
    """A one-page 100 x 100 bp PDF whose only content is a square filled with ``rgb`` (e.g. ``"0 0 1"``)."""
    stream = f"{rgb} rg 10 10 80 80 re f".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Resources << >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


_EMBEDDING_DOCUMENT = (
    "\\documentclass[border=2pt]{standalone}\n"
    "\\def\\pgfsysdriver{pgfsys-dvisvgm.def}\n"
    "\\usepackage[dvisvgm]{graphicx}\n"
    "\\usepackage{tikz}\n"
    "\\graphicspath{{./img/}}\n"
    "\\begin{document}\n"
    "\\begin{tikzpicture}\n"
    "\\node[inner sep=0] (b) {\\includegraphics[bb=0 0 100 100, width=2cm]{board.pdf}};\n"
    "\\draw[red] (b.south west) -- (b.north east);\n"
    "\\end{tikzpicture}\n"
    "\\end{document}\n"
)


def _real_render(tmp_path: Path, document: str):
    source_dir = tmp_path / "staging"
    (source_dir / "img").mkdir(parents=True)
    (source_dir / "img" / "board.pdf").write_bytes(_pdf_with_a_filled_square("0 0 1"))
    result = render_tikz(
        "fig", document, tmp_path / "render" / "fig", tmp_path / "out" / "fig.svg", timeout=120, source_dir=source_dir
    )
    return result, source_dir


@pytest.mark.integration
@pytest.mark.skipif(not (shutil.which("latex") and shutil.which("dvisvgm")), reason="needs latex and dvisvgm")
def test_real_drawing_keeps_the_content_of_an_embedded_pdf(tmp_path: Path):
    result, source_dir = _real_render(tmp_path, _EMBEDDING_DOCUMENT)
    assert result.ok, result.error
    svg = (tmp_path / "out" / "fig.svg").read_text(encoding="utf-8").lower()
    assert any(blue in svg for blue in ("#00f'", '#00f"', "#0000ff", "rgb(0,0,255)")), "the PDF's square is missing"
    assert "#f00" in svg or "#ff0000" in svg  # the TikZ line drawn over it
    assert sorted(p.name for p in source_dir.rglob("*")) == ["board.pdf", "img"]  # nothing written beside sources


@pytest.mark.integration
@pytest.mark.skipif(not (shutil.which("latex") and shutil.which("dvisvgm")), reason="needs latex and dvisvgm")
def test_real_drawing_without_external_graphics_is_unchanged(tmp_path: Path):
    plain = _EMBEDDING_DOCUMENT.replace(
        "\\node[inner sep=0] (b) {\\includegraphics[bb=0 0 100 100, width=2cm]{board.pdf}};\n",
        "\\node[draw, minimum size=2cm] (b) {};\n",
    )
    with_source, _ = _real_render(tmp_path / "a", plain)
    without_source = render_tikz(
        "fig", plain, tmp_path / "b" / "render", tmp_path / "b" / "fig.svg", timeout=120, source_dir=None
    )
    assert with_source.ok and without_source.ok, (with_source.error, without_source.error)
    assert (tmp_path / "a" / "out" / "fig.svg").read_bytes() == (tmp_path / "b" / "fig.svg").read_bytes()


# ---- PDF chain ----------------------------------------------------------------------------------------


def pdf_handler(outcomes: dict[str, str]):
    """``outcomes[tool]``: ok | fail | timeout | malformed | silent."""

    def handler(argv: tuple[str, ...], cwd: Path | None) -> CommandResult:
        tool = Path(argv[0]).name
        kind = outcomes.get(tool, "ok")
        if kind == "fail":
            return failed(argv)
        if kind == "timeout":
            return timed_out(argv)
        if kind == "malformed":
            content = "<svg xmlns='http://www.w3.org/2000/svg'><oops"
        elif kind == "silent":
            return ok_result(argv)
        else:
            content = GOOD_SVG
        if tool == "pdftocairo":
            target = argv[-1]
        elif tool == "inkscape":
            target = next(a.split("=", 1)[1] for a in argv if a.startswith("--export-filename="))
        else:
            target = next(a.split("=", 1)[1] for a in argv if a.startswith("--output="))
        Path(target).write_text(content, encoding="utf-8")
        return ok_result(argv, stderr="WARNING: Couldn't parse text in PDF from UTF16." if tool == "inkscape" else "")

    return handler


def convert(tmp_path: Path, outcomes: dict[str, str], *, which=None, **kwargs):
    which = which or fake_which({"pdftocairo": "/x/pdftocairo", "dvisvgm": "/x/dvisvgm", "inkscape": "/x/inkscape"})
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(PDF_BYTES)
    runner = ScriptedRunner(pdf_handler(outcomes))
    conversion = convert_pdf_to_svg(
        pdf, tmp_path / "out" / "o.svg", tmp_path / "work", timeout=10, which=which, runner=runner, **kwargs
    )
    return conversion, runner


def test_default_order_prefers_pdftocairo_and_stops_at_first_valid(tmp_path: Path):
    assert DEFAULT_PDF_BACKENDS[0] == "pdftocairo"
    conversion, runner = convert(tmp_path, {})
    assert conversion.selected.backend == "pdftocairo" and len(runner.calls) == 1
    assert (tmp_path / "out" / "o.svg").read_text(encoding="utf-8") == GOOD_SVG
    assert runner.calls[0][1] == tmp_path / "work"


@pytest.mark.parametrize("bad", ["fail", "timeout", "malformed", "silent"])
def test_failing_backend_falls_back_to_the_next(tmp_path: Path, bad: str):
    conversion, _ = convert(tmp_path, {"pdftocairo": bad})
    assert conversion.selected.backend == "inkscape"
    first = conversion.attempts[0]
    assert first.backend == "pdftocairo" and not first.success and first.error
    if bad == "malformed":
        assert "not well-formed" in first.error


def test_unavailable_backend_is_recorded_and_skipped(tmp_path: Path):
    which = fake_which({"inkscape": "/x/inkscape"})
    conversion, _ = convert(tmp_path, {}, which=which)
    assert [(a.backend, a.available) for a in conversion.attempts] == [
        ("pdftocairo", False),
        ("inkscape", True),
    ]
    assert conversion.selected.backend == "inkscape"
    assert conversion.attempts[0].error.endswith("not found on PATH")


def test_configured_order_is_respected_and_tool_warnings_are_never_suppressed(tmp_path: Path):
    conversion, _ = convert(tmp_path, {}, backends=("inkscape", "pdftocairo"))
    assert conversion.selected.backend == "inkscape"
    assert any("UTF16" in w for w in conversion.selected.warnings())


def test_all_backends_failing_gives_no_selection_and_no_output(tmp_path: Path):
    conversion, _ = convert(tmp_path, {"pdftocairo": "fail", "dvisvgm": "timeout", "inkscape": "malformed"})
    assert not conversion.ok and len(conversion.attempts) == 3
    assert not (tmp_path / "out" / "o.svg").exists()


def test_compare_all_runs_every_backend_but_selects_the_first_valid(tmp_path: Path):
    conversion, runner = convert(tmp_path, {}, compare_all=True)
    assert len(runner.calls) == 3 and all(a.success for a in conversion.attempts)
    assert conversion.selected.backend == "pdftocairo"


# ---- previews -------------------------------------------------------------------------------------------


def test_preview_unavailable_is_reported_not_raised(tmp_path: Path):
    result = render_preview(tmp_path / "a.svg", tmp_path / "p.png", tmp_path / "w", timeout=5, which=fake_which(set()))
    assert not result.ok and "not found" in result.message


def test_preview_failure_and_success(tmp_path: Path):
    def handler(argv: tuple[str, ...], cwd: Path | None) -> CommandResult:
        if tmp_path.joinpath("fail").exists():
            return failed(argv)
        Path(argv[argv.index("-o") + 1]).write_bytes(b"png")
        return ok_result(argv)

    runner = ScriptedRunner(handler)
    good = render_preview(tmp_path / "a.svg", tmp_path / "p.png", tmp_path / "w", timeout=5, which=WHICH, runner=runner)
    assert good.ok and (tmp_path / "p.png").read_bytes() == b"png"
    (tmp_path / "fail").write_text("x", encoding="utf-8")
    bad = render_preview(tmp_path / "a.svg", tmp_path / "q.png", tmp_path / "w", timeout=5, which=WHICH, runner=runner)
    assert not bad.ok and "failed" in bad.message
