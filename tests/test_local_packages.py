from __future__ import annotations

from pathlib import Path

import pytest
from projectkit import make_dir_link, make_project

from latex2word.source_graph import EdgeKind, NodeKind, Resolution, build_source_graph
from latex2word.staging import staging_dir, stage_files
from latex2word.workspace import Workspace


def graph_of(tmp_path: Path, files: dict[str, str | bytes]):
    root = make_project(tmp_path, files)
    return root, build_source_graph(root, root / "main.tex")


def codes(graph) -> list[str]:
    return [f.code for f in graph.findings]


def test_local_class_is_discovered_and_staged(tmp_path: Path):
    _, graph = graph_of(tmp_path, {"main.tex": "\\documentclass{localclass}\n", "localclass.cls": "\\ProvidesClass{x}"})
    assert graph.package_files == ["localclass.cls"]
    assert graph.stageable_files() == [("main.tex", "main"), ("localclass.cls", "package")]
    assert graph.tex_files == ["main.tex"]
    assert "localclass.cls" in graph.files
    assert graph.nodes["localclass.cls"].kind is NodeKind.PACKAGE
    assert [e.kind for e in graph.edges] == [EdgeKind.PACKAGE]
    assert not graph.findings


def test_local_style_with_options_and_subdirectory(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {"main.tex": "\\documentclass{article}\n\\usepackage[draft]{styles/foo}\n", "styles/foo.sty": ""},
    )
    assert graph.package_files == ["styles/foo.sty"]
    assert not graph.findings


def test_package_list_only_local_member_is_followed_silently(tmp_path: Path):
    _, graph = graph_of(tmp_path, {"main.tex": "\\usepackage[opt]{a,b}\n", "a.sty": ""})
    assert graph.package_files == ["a.sty"]
    assert not graph.findings
    assert "b.sty" not in graph.nodes


def test_transitive_package_follows_parent_in_order(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {"main.tex": "\\usepackage{outer}\n", "outer.sty": "\\RequirePackage[x]{inner}", "inner.sty": "\\LoadClass{base}"},
    )
    assert graph.stageable_files() == [("main.tex", "main"), ("outer.sty", "package"), ("inner.sty", "package")]


def test_package_cycle_terminates(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {"main.tex": "\\usepackage{a}", "a.sty": "\\RequirePackage{b}", "b.sty": "\\RequirePackage{a}"},
    )
    assert graph.package_files == ["a.sty", "b.sty"]
    assert not graph.findings


def test_system_packages_produce_nothing(tmp_path: Path):
    _, graph = graph_of(tmp_path, {"main.tex": "\\documentclass{article}\\usepackage{amsmath,graphicx}\n"})
    assert graph.package_files == []
    assert not graph.findings
    assert list(graph.nodes) == ["main.tex"]
    assert graph.edges == []


def test_package_text_is_not_scanned_for_figures_or_includes(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {
            "main.tex": "\\usepackage{a}",
            "a.sty": "\\includegraphics{x.png}\\input{other}\\bibliography{refs}",
            "x.png": b"png",
            "other.tex": "",
            "refs.bib": "",
        },
    )
    # Audit 2026-11 (reports/audit/findings.md, F-02 follow-up): Pandoc loads local .sty files, so their \input and
    # \includegraphics are followed and contained like the document's own (they used to be ignored, which let a
    # .sty read files outside the project). \bibliography in a package is still not followed.
    assert [e.kind for e in graph.edges] == [EdgeKind.PACKAGE, EdgeKind.GRAPHIC, EdgeKind.INPUT]
    assert graph.tex_files == ["main.tex", "other.tex"]
    assert not graph.findings


def test_parent_dir_package_is_outside_root(tmp_path: Path):
    (tmp_path / "evil.sty").write_text("", encoding="utf-8")
    _, graph = graph_of(tmp_path, {"main.tex": "\\usepackage{../evil}\n"})
    assert "outside-root" in codes(graph)
    assert graph.package_files == []
    assert "evil.sty" not in graph.files


def test_symlink_escaping_package_is_not_read(tmp_path: Path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.sty").write_text("", encoding="utf-8")
    root = make_project(tmp_path, {"main.tex": "\\usepackage{linked/secret}\n"})
    if not make_dir_link(root / "linked", external):
        pytest.skip("cannot create symlinks or junctions")
    graph = build_source_graph(root, root / "main.tex")
    assert "outside-root" in codes(graph)
    assert graph.package_files == []
    assert graph.with_resolution(Resolution.OUTSIDE_ROOT)


def test_package_files_are_staged(tmp_path: Path):
    root, graph = graph_of(
        tmp_path, {"main.tex": "\\documentclass{c}\\usepackage{s}", "c.cls": "\\RequirePackage{s}", "s.sty": "x"}
    )
    workspace = Workspace.create(tmp_path / "ws", root)
    manifest = stage_files(root, graph.stageable_files(), workspace)
    assert (staging_dir(workspace) / "c.cls").is_file() and (staging_dir(workspace) / "s.sty").is_file()
    assert manifest.by_source("s.sty").dependency_type == "package"


def test_addbibresource_resolves_with_options_and_extension(tmp_path: Path):
    _, graph = graph_of(
        tmp_path,
        {"main.tex": "\\addbibresource{refs.bib}\\addbibresource[label=x]{more}", "refs.bib": "", "more.bib": ""},
    )
    assert graph.bibliography_files == ["refs.bib", "more.bib"]
    assert not graph.findings
    assert all(e.kind is EdgeKind.BIBLIOGRAPHY for e in graph.edges)


def test_addbibresource_missing_file_is_an_error(tmp_path: Path):
    _, graph = graph_of(tmp_path, {"main.tex": "\\addbibresource{gone.bib}"})
    assert [e.raw for e in graph.missing] == ["gone.bib"]
    assert codes(graph) == ["missing-dependency"]
    assert "unsupported-command" not in codes(graph)


def test_addbibresource_remote_is_info_without_edge(tmp_path: Path):
    _, graph = graph_of(tmp_path, {"main.tex": "\\addbibresource[location=remote]{https://example.org/x.bib}"})
    assert codes(graph) == ["remote-bibliography"]
    assert graph.edges == []
    assert not graph.has_errors


def test_addbibresource_dynamic_name_warns(tmp_path: Path):
    _, graph = graph_of(tmp_path, {"main.tex": "\\addbibresource{\\jobname.bib}"})
    assert codes(graph) == ["dynamic-path"]
    assert graph.edges[0].resolution is Resolution.DYNAMIC
