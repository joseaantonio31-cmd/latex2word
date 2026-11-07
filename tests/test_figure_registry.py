from __future__ import annotations

from pathlib import Path

from projectkit import JPEG_BYTES, PDF_BYTES, PNG_BYTES, make_project

from latex2word.figure_registry import discover_figures
from latex2word.source_graph import build_source_graph

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><path d="M0 0L1 1"/></svg>'


def discover(tmp_path: Path, files: dict[str, str | bytes]):
    root = make_project(tmp_path, files)
    graph = build_source_graph(root, root / "main.tex")
    return discover_figures(graph, root, root / "main.tex")


def test_raster_vector_and_pdf_graphics_are_classified(tmp_path: Path):
    registry = discover(
        tmp_path,
        {
            "main.tex": "\\input{figs}",
            "figs.tex": (
                "\\begin{figure}\\includegraphics[width=3cm]{a.png}\\label{fig:a}\\end{figure}\n"
                "\\includegraphics{b.jpg}\\includegraphics{c.jpeg}\\includegraphics{d.pdf}\\includegraphics{e.svg}"
            ),
            "a.png": PNG_BYTES,
            "b.jpg": JPEG_BYTES,
            "c.jpeg": JPEG_BYTES,
            "d.pdf": PDF_BYTES,
            "e.svg": SVG,
        },
    )
    assert [(f.id, f.source_type, f.planned_renderer, f.output) for f in registry.figures] == [
        ("a", "png", "copy", "figures/a.png"),
        ("b", "jpeg", "copy", "figures/b.jpg"),
        ("c", "jpeg", "copy", "figures/c.jpeg"),
        ("d", "pdf", "pdf-to-svg", "figures/d.svg"),
        ("e", "svg", "copy", "figures/e.svg"),
    ]
    first = registry.figures[0]
    assert (first.label, first.environment, first.requested_width, first.line) == ("fig:a", "figure", "3cm", 1)
    assert first.input_asset == "a.png" and first.source_sha256 and first.active


def test_tikz_and_circuitikz_are_detected_with_resizebox_width(tmp_path: Path):
    registry = discover(
        tmp_path,
        {
            "main.tex": "\\input{t}",
            "t.tex": (
                "\\begin{figure*}\\resizebox{0.9\\textwidth}{!}{%\n\\begin{tikzpicture}\\draw (0,0)--(1,1);"
                "\\end{tikzpicture}%\n}\\caption{c}\\label{fig:t}\\end{figure*}\n"
                "\\begin{circuitikz}\\draw (0,0) to[R] (2,0);\\end{circuitikz}"
            ),
        },
    )
    types = [(f.source_type, f.environment, f.requested_width, f.label) for f in registry.figures]
    assert types == [("tikzpicture", "figure*", "0.9\\textwidth", "fig:t"), ("circuitikz", None, None, None)]
    assert [f.id for f in registry.figures] == ["t", "t-2"]
    assert all(f.input_asset is None and f.source_sha256 and f.span for f in registry.figures)


def test_unreachable_wrapper_is_inactive_and_commented_figures_do_not_exist(tmp_path: Path):
    registry = discover(
        tmp_path,
        {
            "main.tex": "\\input{figures/live}\n% \\input{figures/commented}\n",
            "figures/live.tex": "\\begin{circuitikz}\\end{circuitikz}",
            "figures/orphan.tex": "\\begin{figure}\\begin{circuitikz}\\end{circuitikz}\\end{figure}",
            "figures/commented.tex": "\\begin{tikzpicture}\\end{tikzpicture}",
            "elsewhere/deep.tex": "\\begin{tikzpicture}\\end{tikzpicture}",
        },
    )
    assert [(f.id, f.active) for f in registry.figures] == [
        ("live", True),
        ("commented", False),
        ("orphan", False),
    ]
    assert [f.id for f in registry.active] == ["live"]
    # directories without any reachable file are not crawled
    assert registry.by_id("deep") is None


def test_figure_inside_a_comment_or_iffalse_is_ignored(tmp_path: Path):
    registry = discover(
        tmp_path,
        {
            "main.tex": "% \\begin{tikzpicture}\\end{tikzpicture}\n\\iffalse\\includegraphics{a.png}\\fi\n",
            "a.png": PNG_BYTES,
        },
    )
    assert registry.figures == []


def test_malformed_active_tikz_is_an_error_finding(tmp_path: Path):
    registry = discover(tmp_path, {"main.tex": "\\begin{tikzpicture}\\draw (0,0);"})
    assert [f.code for f in registry.findings] == ["malformed-environment"]
    assert registry.figures == []


def test_ids_are_unique_and_stable_for_repeated_assets(tmp_path: Path):
    registry = discover(
        tmp_path, {"main.tex": "\\includegraphics{a.png}\\includegraphics{a.png}", "a.png": PNG_BYTES}
    )
    assert [f.id for f in registry.figures] == ["a", "a-2"]


def test_unresolved_graphics_do_not_become_figures(tmp_path: Path):
    registry = discover(tmp_path, {"main.tex": "\\includegraphics{missing}"})
    assert registry.figures == []


# ---- graphics inside a drawing -------------------------------------------------------------------


def test_graphic_inside_a_tikz_drawing_is_part_of_that_single_figure(tmp_path: Path):
    root = make_project(
        tmp_path,
        {
            "main.tex": (
                "\\begin{figure}\\begin{tikzpicture}\\node (b) {\\includegraphics[width=3cm]{img/board.pdf}};"
                "\\draw (b.south west) -- (b.north east);\\end{tikzpicture}\\label{fig:b}\\end{figure}"
            ),
            "img/board.pdf": PDF_BYTES,
        },
    )
    graph = build_source_graph(root, root / "main.tex")
    registry = discover_figures(graph, root, root / "main.tex")
    assert [(f.id, f.source_type, f.label, f.input_asset) for f in registry.figures] == [
        ("main", "tikzpicture", "fig:b", None)
    ]
    # the graphic is still a resolved dependency, so it is staged for the drawing's render
    assert ("img/board.pdf", "graphic") in graph.stageable_files()


def test_standalone_graphics_next_to_drawings_keep_their_own_records(tmp_path: Path):
    registry = discover(
        tmp_path,
        {
            "main.tex": (
                "\\begin{tikzpicture}\\node {\\includegraphics{a.png}};\\end{tikzpicture}\n"
                "\\begin{figure}\\includegraphics{a.png}\\label{fig:a}\\end{figure}\n"
                "\\begin{circuitikz}\\draw (0,0) to[R] (2,0);\\end{circuitikz}\n"
                "\\includegraphics{d.pdf}"
            ),
            "a.png": PNG_BYTES,
            "d.pdf": PDF_BYTES,
        },
    )
    assert [(f.id, f.source_type, f.label) for f in registry.figures] == [
        ("main", "tikzpicture", None),
        ("a", "png", "fig:a"),
        ("main-2", "circuitikz", None),
        ("d", "pdf", None),
    ]
