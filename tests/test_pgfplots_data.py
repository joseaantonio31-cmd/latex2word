"""pgfplots data files and project siunitx units in rendered drawings.

Regression for: figures whose ``\\addplot table {dir/data.csv}`` paths are
relative to the main file failed to render because the data files were never
staged and ``latex`` ran in an isolated render directory; and drawings using a
project ``\\DeclareSIUnit`` unit failed with "Undefined control sequence".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import fake_which
from projectkit import GOOD_SVG, ScriptedRunner, make_project, ok_result

from latex2word.process import CommandResult
from latex2word.source_graph import EdgeKind, NodeKind, Resolution, build_source_graph
from latex2word.tikz import derive_render_preamble
from latex2word.tikz_render import render_tikz

PLOT = r"""\begin{tikzpicture}\begin{axis}
\addplot[thick, blue] table[x=f, y=s21] {Figuras/datos/a.csv};
\addplot+ [mark=*] table {Figuras/datos/b.csv};
\addplot3 file {Figuras/datos/c.dat};
\addplot table[row sep=\\] {x y \\ 1 2 \\};
\addplot {x^2};
\addplot coordinates {(0,0) (1,1)};
\pgfplotstableread[col sep=comma]{Figuras/datos/d.csv}\loadedtable
\addplot table[x=t, y=v] {\loadedtable};
\end{axis}\end{tikzpicture}
"""


def graph_of(tmp_path: Path, files: dict[str, str | bytes]):
    root = make_project(tmp_path, files)
    return build_source_graph(root, root / "doc" / "main.tex")


def test_pgfplots_data_files_are_dependencies_resolved_from_the_main_directory(tmp_path: Path):
    graph = graph_of(
        tmp_path,
        {
            "doc/main.tex": "\\documentclass{report}\\begin{document}\\input{Figuras/plot.tex}\\end{document}",
            "doc/Figuras/plot.tex": PLOT,
            "doc/Figuras/datos/a.csv": "f s21\n1 2\n",
            "doc/Figuras/datos/b.csv": "x y\n1 2\n",
            "doc/Figuras/datos/c.dat": "1 2 3\n",
            "doc/Figuras/datos/d.csv": "t,v\n1,2\n",
        },
    )
    data = sorted(n.id for n in graph.resolved_nodes(NodeKind.DATA))
    assert data == [f"doc/Figuras/datos/{n}" for n in ("a.csv", "b.csv", "c.dat", "d.csv")]
    assert {("doc/Figuras/datos/a.csv", "data")} <= set(graph.stageable_files())
    edges = graph.edges_of(EdgeKind.DATA)
    # inline data, expressions, coordinates and the \loadedtable macro are not file dependencies
    assert len(edges) == 4 and all(e.resolution is Resolution.OK for e in edges)
    assert not graph.has_errors


def test_missing_pgfplots_data_file_is_a_dependency_error(tmp_path: Path):
    graph = graph_of(
        tmp_path,
        {"doc/main.tex": "\\documentclass{report}\\begin{document}\\addplot table {datos/none.csv};\\end{document}"},
    )
    assert graph.has_errors
    assert any(f.code == "missing-dependency" and "none.csv" in f.message for f in graph.findings)


def test_render_preamble_carries_project_siunitx_configuration():
    main = (
        "\\documentclass{report}\n\\usepackage{siunitx}\n\\sisetup{\n  per-mode=symbol\n}\n"
        "\\DeclareSIUnit{\\sample}{Sa}\n\\DeclareSIUnit[number-unit-product=\\,]{\\Vpp}{V_{pp}}\n"
        "\\usepackage{tikz}\n\\begin{document}\n\\DeclareSIUnit{\\late}{L}\n\\end{document}\n"
    )
    lines = derive_render_preamble(main, "\\node{\\SI{1}{\\sample}};").lines
    assert "\\DeclareSIUnit{\\sample}{Sa}" in lines
    assert "\\DeclareSIUnit[number-unit-product=\\,]{\\Vpp}{V_{pp}}" in lines
    assert any(line.startswith("\\sisetup{") for line in lines)
    assert not any("late" in line for line in lines)  # body definitions are not preamble


def _handler(argv: tuple[str, ...], cwd: Path | None) -> CommandResult:
    assert cwd is not None
    if argv[0].endswith("latex"):
        out = next(a.split("=", 1)[1] for a in argv if a.startswith("-output-directory="))
        (cwd / out / Path(argv[-1]).name).with_suffix(".dvi").write_bytes(b"dvi")
        return ok_result(argv)
    Path(next(a for a in argv if a.startswith("--output="))[len("--output=") :]).write_text(GOOD_SVG, encoding="utf-8")
    return ok_result(argv)


def test_latex_runs_in_the_staged_main_directory_and_writes_into_the_render_directory(tmp_path: Path):
    source_dir = tmp_path / "staging" / "doc"
    source_dir.mkdir(parents=True)
    workdir = tmp_path / "render" / "fig"
    runner = ScriptedRunner(_handler)
    result = render_tikz(
        "fig", "\\documentclass{standalone}", workdir, tmp_path / "out" / "fig.svg",
        timeout=10, which=fake_which({"latex", "dvisvgm"}), runner=runner, source_dir=source_dir,
    )
    assert result.ok, result.error
    (latex_argv, latex_cwd), (_svg_argv, svg_cwd) = runner.calls
    assert latex_cwd == source_dir and svg_cwd == workdir
    assert "-output-directory=../../render/fig" in latex_argv
    assert latex_argv[-1] == "../../render/fig/fig.tex"
    assert not any(source_dir.iterdir())  # nothing written next to the staged sources


@pytest.mark.parametrize("missing", [True])
def test_without_source_dir_latex_still_runs_in_the_render_directory(tmp_path: Path, missing: bool):
    calls: list[Path | None] = []

    def handler(argv: tuple[str, ...], cwd: Path | None) -> CommandResult:
        calls.append(cwd)
        if argv[0].endswith("latex"):
            assert not any(a.startswith("-output-directory") for a in argv)
            (cwd / argv[-1]).with_suffix(".dvi").write_bytes(b"dvi")
        else:
            Path(next(a for a in argv if a.startswith("--output="))[9:]).write_text(GOOD_SVG, encoding="utf-8")
        return ok_result(argv)

    result = render_tikz(
        "fig", "x", tmp_path / "w", tmp_path / "o.svg", timeout=10,
        which=fake_which({"latex", "dvisvgm"}), runner=ScriptedRunner(handler),
        source_dir=(tmp_path / "absent") if missing else None,
    )
    assert result.ok and calls == [tmp_path / "w", tmp_path / "w"]
