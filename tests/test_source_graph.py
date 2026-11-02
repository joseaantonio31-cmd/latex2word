from __future__ import annotations

from pathlib import Path

import pytest
from projectkit import PDF_BYTES, make_dir_link, make_project

from latex2word.diagnostics import Severity
from latex2word.latex_scan import (
    LineIndex,
    iter_commands,
    mask_non_live,
    read_group,
    scan_environments,
    split_top_level,
)
from latex2word.source_graph import EdgeKind, NodeKind, Resolution, build_source_graph


def graph_of(tmp_path: Path, files: dict[str, str | bytes], main: str = "main.tex"):
    root = make_project(tmp_path, files)
    return root, build_source_graph(root, root / main)


def codes(graph) -> list[str]:
    return [f.code for f in graph.findings]


# ---- lexical layer ----------------------------------------------------------------------------


def test_mask_blanks_comments_but_keeps_length_and_newlines():
    text = "a % \\input{x}\nb \\% \\input{y}\n"
    masked = mask_non_live(text).text
    assert len(masked) == len(text)
    assert masked.count("\n") == text.count("\n")
    assert "\\input{x}" not in masked
    assert "\\input{y}" in masked  # \% is an escaped percent, not a comment


def test_mask_double_backslash_then_percent_is_a_comment():
    assert "input" not in mask_non_live("\\\\% \\input{x}").text


@pytest.mark.parametrize(
    "block",
    [
        "\\begin{verbatim}\\input{x}\\end{verbatim}",
        "\\begin{comment}\n\\input{x}\n\\end{comment}",
        "\\iffalse \\input{x} \\fi",
        "\\verb|\\input{x}|",
    ],
)
def test_mask_hides_non_live_regions(block: str):
    assert "\\input{x}" not in mask_non_live(f"live {block} live").text


def test_iffalse_with_nested_conditionals_ends_at_matching_fi():
    text = "\\iffalse \\ifx a b \\input{hidden} \\fi \\input{also-hidden} \\fi \\input{live}"
    masked = mask_non_live(text).text
    assert "hidden" not in masked
    assert "\\input{live}" in masked


def test_unterminated_iffalse_is_reported_and_masked_to_end():
    result = mask_non_live("\\iffalse \\input{x}")
    assert result.problems and "x" not in result.text


def test_read_group_handles_nested_and_escaped_braces():
    text = "{a {b} \\} c} tail"
    start, end, after = read_group(text, 0)
    assert text[start:end] == "a {b} \\} c"
    assert read_group("{unclosed", 0) is None


def test_split_top_level_ignores_commas_in_braces():
    assert split_top_level("width=1cm, caption={a,b}, x") == ["width=1cm", "caption={a,b}", "x"]


def test_commands_and_line_numbers():
    text = "a\n\\input{x}\n\\\\ \\foo*"
    names = [(c.name, c.starred) for c in iter_commands(text)]
    assert names == [("input", False), ("\\", False), ("foo", True)]
    assert LineIndex(text).line(text.index("\\input")) == 2


def test_scan_environments_nesting_and_problems():
    text = "\\begin{a}x\\begin{a}y\\end{a}z\\end{a} \\begin{a}2\\end{a} \\begin{a}open"
    found, problems = scan_environments(text, ["a"])
    assert [text[e.content_start : e.content_end] for e in found] == ["x\\begin{a}y\\end{a}z", "2"]
    assert len(problems) == 1 and "unclosed" in problems[0].message


# ---- source graph ---------------------------------------------------------------------------------


def test_comments_are_not_live(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {
            "main.tex": "% \\input{ghost}\n\\input{real} % \\input{ghost2}\n",
            "real.tex": "",
        },
    )
    assert graph.tex_files == ["main.tex", "real.tex"]
    assert graph.findings == []


def test_nested_input_preserves_dependency_order_and_locations(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {
            "main.tex": "\\input{a}\n\n\\include{c}\n",
            "a.tex": "x\n\\input{sub/b}\n",
            "sub/b.tex": "",
            "c.tex": "\\input{sub/b}",
        },
    )
    assert graph.tex_files == ["main.tex", "a.tex", "sub/b.tex", "c.tex"]
    kinds = [(e.kind, e.source, e.target, str(e.location)) for e in graph.edges]
    assert kinds == [
        (EdgeKind.INPUT, "main.tex", "a.tex", "main.tex:1"),
        (EdgeKind.INPUT, "a.tex", "sub/b.tex", "a.tex:2"),
        (EdgeKind.INCLUDE, "main.tex", "c.tex", "main.tex:3"),
        (EdgeKind.INPUT, "c.tex", "sub/b.tex", "c.tex:1"),
    ]
    assert graph.nodes["main.tex"].kind is NodeKind.MAIN


def test_references_resolve_against_main_directory_not_including_file(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {"main.tex": "\\input{sections/a}", "sections/a.tex": "\\input{b}", "sections/b.tex": "", "b.tex": ""},
    )
    assert graph.tex_files == ["main.tex", "sections/a.tex", "b.tex"]


def test_main_in_subdirectory_is_the_tex_working_directory(tmp_path: Path):
    _, graph = graph_of(
        tmp_path, {"paper/main.tex": "\\input{parts/a}", "paper/parts/a.tex": ""}, main="paper/main.tex"
    )
    assert graph.tex_files == ["paper/main.tex", "paper/parts/a.tex"]


def test_include_cycle_is_detected_without_looping(tmp_path: Path):
    _, graph = graph_of(tmp_path, {"main.tex": "\\input{a}", "a.tex": "\\input{b}", "b.tex": "\\input{a}"})
    assert "include-cycle" in codes(graph)
    cycle = next(f for f in graph.findings if f.code == "include-cycle")
    assert cycle.severity is Severity.ERROR and "a.tex -> b.tex -> a.tex" in cycle.message
    assert graph.tex_files == ["main.tex", "a.tex", "b.tex"]


def test_self_include_is_a_cycle(tmp_path: Path):
    _, graph = graph_of(tmp_path, {"main.tex": "\\input{main}"})
    assert "include-cycle" in codes(graph)


def test_missing_input_is_an_error_with_location(tmp_path: Path):
    _, graph = graph_of(tmp_path, {"main.tex": "\n\\input{nope}\n"})
    [finding] = [f for f in graph.findings if f.code == "missing-dependency"]
    assert finding.severity is Severity.ERROR and str(finding.location) == "main.tex:2"
    assert [e.raw for e in graph.missing] == ["nope"]
    assert graph.has_errors


def test_missing_main_is_reported(tmp_path: Path):
    root = make_project(tmp_path, {})
    graph = build_source_graph(root, root / "main.tex")
    assert codes(graph) == ["main-missing"]


@pytest.mark.parametrize("reference", ["part", "part.tex"])
def test_extensionless_and_explicit_tex_references_resolve(tmp_path: Path, reference: str):
    _, graph = graph_of(tmp_path, {"main.tex": f"\\input{{{reference}}}", "part.tex": ""})
    assert graph.tex_files == ["main.tex", "part.tex"]


def test_duplicate_references_scan_once_but_keep_both_edges(tmp_path: Path):
    _, graph = graph_of(tmp_path, {"main.tex": "\\input{a}\\input{a}", "a.tex": "\\includegraphics{f.png}", "f.png": b"x"})
    assert graph.tex_files == ["main.tex", "a.tex"]
    assert len(graph.edges_of(EdgeKind.INPUT)) == 2
    assert len(graph.edges_of(EdgeKind.GRAPHIC)) == 1
    assert "duplicate-reference" in codes(graph)


def test_parent_escape_is_reported_and_never_read(tmp_path: Path):
    (tmp_path / "outside.tex").write_text("\\input{secret}", encoding="utf-8")
    _, graph = graph_of(tmp_path, {"main.tex": "\\input{../outside}"})
    assert [e.resolution for e in graph.outside_root] == [Resolution.OUTSIDE_ROOT]
    assert "outside-root" in codes(graph)
    assert graph.tex_files == ["main.tex"]


def test_absolute_path_outside_root_is_reported(tmp_path: Path):
    outside = tmp_path / "elsewhere.tex"
    outside.write_text("", encoding="utf-8")
    _, graph = graph_of(tmp_path, {"main.tex": f"\\input{{{outside.as_posix()}}}"})
    assert [e.resolution for e in graph.outside_root] == [Resolution.OUTSIDE_ROOT]


def test_junction_or_symlink_leaving_the_root_is_reported(tmp_path: Path):
    target = tmp_path / "external"
    target.mkdir()
    (target / "leak.tex").write_text("leaked", encoding="utf-8")
    root = make_project(tmp_path, {"main.tex": "\\input{linked/leak}"})
    if not make_dir_link(root / "linked", target):
        pytest.skip("cannot create a directory link on this machine")
    graph = build_source_graph(root, root / "main.tex")
    assert [e.resolution for e in graph.outside_root] == [Resolution.OUTSIDE_ROOT]
    assert graph.tex_files == ["main.tex"]


def test_graphicspath_and_extensionless_graphics(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {
            "main.tex": "\\graphicspath{{img/}{more}}\n\\includegraphics[width=0.5\\linewidth]{plot}\n\\includegraphics{b}\n",
            "img/plot.pdf": PDF_BYTES,
            "more/b.png": b"png",
        },
    )
    graphics = graph.edges_of(EdgeKind.GRAPHIC)
    assert [(e.target, e.resolution) for e in graphics] == [
        ("img/plot.pdf", Resolution.OK),
        ("more/b.png", Resolution.OK),
    ]
    assert graphics[0].options == {"width": "0.5\\linewidth"}
    assert graph.nodes["img/plot.pdf"].source_type == "pdf"


def test_graphics_extension_preference_and_ambiguity_note(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {"main.tex": "\\includegraphics{f}", "f.png": b"p", "f.pdf": PDF_BYTES},
    )
    assert graph.edges_of(EdgeKind.GRAPHIC)[0].target == "f.pdf"
    assert "ambiguous-graphic" in codes(graph)


def test_missing_and_unsupported_graphics(tmp_path: Path):
    _, graph = graph_of(
        tmp_path, {"main.tex": "\\includegraphics{gone}\\includegraphics{pic.eps}", "pic.eps": b"%!PS"}
    )
    assert [e.resolution for e in graph.edges_of(EdgeKind.GRAPHIC)] == [Resolution.MISSING, Resolution.UNSUPPORTED]
    assert {"missing-dependency", "unsupported-graphic-type"} <= set(codes(graph))


def test_bibliography_list_and_missing_entry(tmp_path: Path):
    _, graph = graph_of(tmp_path, {"main.tex": "\\bibliography{refs,other.bib}", "refs.bib": "@a{x,}"})
    assert graph.bibliography_files == ["refs.bib"]
    assert [e.raw for e in graph.missing] == ["other.bib"]


def test_dynamic_and_unsupported_constructs_produce_diagnostics_not_guesses(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {
            "main.tex": "\\input{sec/\\jobname}\n\\input foo\n\\subfile{x}\n\\includegraphics{\\figdir/a}\n",
            "foo.tex": "",
        },
    )
    assert graph.tex_files == ["main.tex"]
    assert codes(graph).count("dynamic-path") == 2
    assert {"unsupported-syntax", "unsupported-command"} <= set(codes(graph))
    assert not graph.has_errors


def test_nested_braces_in_option_and_path_arguments(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {"main.tex": "\\includegraphics[width={0.5\\linewidth}, caption={a,b}]{a.png}", "a.png": b"x"},
    )
    assert graph.edges_of(EdgeKind.GRAPHIC)[0].options["width"] == "{0.5\\linewidth}"
    assert graph.edges_of(EdgeKind.GRAPHIC)[0].target == "a.png"


def test_commands_inside_verbatim_and_iffalse_are_not_followed(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {"main.tex": "\\begin{verbatim}\\input{a}\\end{verbatim}\n\\iffalse\\input{b}\\fi\n\\input{c}", "c.tex": ""},
    )
    assert graph.tex_files == ["main.tex", "c.tex"]


def test_scanning_never_writes_to_the_project(tmp_path: Path):
    from projectkit import tree_digest

    root, _ = graph_of(tmp_path, {"main.tex": "\\input{a}", "a.tex": "\\includegraphics{x.png}", "x.png": b"x"})
    before = tree_digest(root)
    build_source_graph(root, root / "main.tex")
    assert tree_digest(root) == before
    assert sorted(p.name for p in root.iterdir()) == ["a.tex", "main.tex", "x.png"]
