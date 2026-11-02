"""F-06: the output DOCX may never replace a build input or another non-DOCX file (reports/audit/remediation-phase2.md)."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest
import yaml
from projectkit import make_project

from latex2word.output_safety import canonical, check_output_target, protected_inputs
from latex2word.source_graph import build_source_graph

MAIN = (
    "\\documentclass{article}\n\\usepackage{local}\n\\usepackage{graphicx}\n\\begin{document}\n"
    "\\input{chapters/one}\n\\includegraphics{fig.png}\n\\bibliography{refs}\n\\end{document}\n"
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(
        tmp_path,
        {"main.tex": MAIN, "chapters/one.tex": "Text.\n", "local.sty": "\\newcommand\\x{y}\n",
         "fig.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "refs.bib": "@book{k,title={T}}\n"},
    )


def codes(project: Path, target: Path, *, output_dir: Path | None = None, extra=(), case_insensitive=None) -> list[str]:
    graph = build_source_graph(project, project / "main.tex")
    findings = check_output_target(
        target, project_root=project, output_directory=output_dir or project / "output",
        inputs=protected_inputs(graph, extra), case_insensitive=case_insensitive,
    )
    return sorted({f.code for f in findings})


def test_every_resolved_input_is_protected(project: Path):
    graph = build_source_graph(project, project / "main.tex")
    names = sorted(p.relative_to(project).as_posix() for p in protected_inputs(graph))
    assert names == ["chapters/one.tex", "fig.png", "local.sty", "main.tex", "refs.bib"]


@pytest.mark.parametrize("name", ["main.tex", "refs.bib", "local.sty", "chapters/one.tex", "fig.png"])
def test_an_input_is_never_a_valid_target(project: Path, name: str):
    assert "L2W-E-OUTPUT-002" in codes(project, project / name)


def test_output_main_tex_reports_every_reason(project: Path):
    assert codes(project, project / "main.tex") == [
        "L2W-E-OUTPUT-001", "L2W-E-OUTPUT-002", "L2W-E-OUTPUT-003", "L2W-E-OUTPUT-004",
    ]


def test_configuration_file_is_protected(tmp_path: Path, project: Path):
    config = tmp_path / "config.docx"  # an absurd but possible name: protected by identity, not by suffix
    config.write_text("schema_version: 1\n", encoding="utf-8")
    assert "L2W-E-OUTPUT-002" in codes(project, config, extra=[config])


def test_existing_non_docx_file_outside_the_project_is_not_overwritten(tmp_path: Path, project: Path):
    existing = tmp_path / "existing.txt"
    existing.write_text("keep me\n", encoding="utf-8")
    assert codes(project, existing) == ["L2W-E-OUTPUT-001", "L2W-E-OUTPUT-003"]
    fake = tmp_path / "report.docx"
    fake.write_text("not a zip\n", encoding="utf-8")
    assert codes(project, fake) == ["L2W-E-OUTPUT-003"]


def test_directory_target_is_refused(tmp_path: Path, project: Path):
    (tmp_path / "dir.docx").mkdir()
    assert codes(project, tmp_path / "dir.docx") == ["L2W-E-OUTPUT-003"]


def test_safe_targets(tmp_path: Path, project: Path):
    assert codes(project, tmp_path / "elsewhere" / "out.docx") == []
    previous = tmp_path / "previous.docx"
    with zipfile.ZipFile(previous, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
    assert codes(project, previous) == []  # replacing an earlier DOCX is the purpose of the build


def test_configured_generated_output_directory_inside_the_project(project: Path):
    assert codes(project, project / "output" / "main.docx", output_dir=project / "output") == []
    assert codes(project, project / "chapters" / "main.docx", output_dir=project / "output") == ["L2W-E-OUTPUT-004"]


def test_output_directory_holding_sources_is_not_a_generated_directory(project: Path):
    assert codes(project, project / "chapters" / "x.docx", output_dir=project / "chapters") == ["L2W-E-OUTPUT-005"]


def test_relative_alias_of_a_source_is_recognised(project: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(project / "chapters")
    assert "L2W-E-OUTPUT-002" in codes(project, Path("..") / "chapters" / ".." / "main.tex")


def test_case_variant_matches_only_under_case_insensitive_semantics(project: Path):
    variant = project / "MAIN.TEX"
    assert "L2W-E-OUTPUT-002" in codes(project, variant, case_insensitive=True)
    if os.path.exists(variant):
        # case-insensitive file system: MAIN.TEX *is* main.tex, and realpath returns the stored spelling, so the
        # input is recognised even without case folding (the safe answer)
        assert "L2W-E-OUTPUT-002" in codes(project, variant, case_insensitive=False)
    else:
        assert "L2W-E-OUTPUT-002" not in codes(project, variant, case_insensitive=False)
    assert canonical("C:/A/B.docx", case_insensitive=True) == canonical("c:/a/b.DOCX", case_insensitive=True)


def test_symlink_to_a_source_is_recognised(tmp_path: Path, project: Path):
    link = tmp_path / "innocent.docx"
    try:
        link.symlink_to(project / "main.tex")
    except OSError:
        pytest.skip("symlinks not available")
    assert {"L2W-E-OUTPUT-002", "L2W-E-OUTPUT-003"} <= set(codes(project, link))


def test_symlinked_directory_escape_into_the_project_is_recognised(tmp_path: Path, project: Path):
    door = tmp_path / "door"
    try:
        door.symlink_to(project / "chapters", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not available")
    assert "L2W-E-OUTPUT-004" in codes(project, door / "out.docx")  # really project/chapters/out.docx


def test_build_refuses_before_any_conversion_work(tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch):
    import latex2word.build as build_module
    from latex2word.config import load_config
    from latex2word.errors import ExitCode
    from latex2word.pandoc_adapter import PandocTool
    from latex2word.pandoc_compat import PandocAssessment, PandocCompat

    monkeypatch.setattr(build_module, "locate_pandoc", lambda **_: PandocTool("/fake/pandoc", "3.12", "pandoc 3.12"))
    monkeypatch.setattr(build_module, "assess_pandoc",
                        lambda version, **_: PandocAssessment(PandocCompat.SUPPORTED, version, (), ()))
    monkeypatch.setattr(build_module, "run_figures", lambda *a, **k: pytest.fail("conversion started"))
    data = {"schema_version": 1, "project": {"name": "p", "root": str(project), "main": "main.tex"},
            "profile": "generic", "workspace": {"root": str(tmp_path / "ws")}, "output": {"directory": str(tmp_path / "out")}}
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    before = (project / "main.tex").read_bytes()
    with pytest.raises(build_module.OutputSafetyError) as raised:
        build_module.run_build(load_config(tmp_path / "c.yaml"), output=project / "main.tex")
    assert raised.value.exit_code is ExitCode.ERROR
    assert "L2W-E-OUTPUT-002" in {f.code for f in raised.value.findings}
    assert (project / "main.tex").read_bytes() == before and not (tmp_path / "ws").exists()


def test_cli_exit_code_is_2_and_the_source_survives(tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    import latex2word.build as build_module
    from latex2word.cli import main
    from latex2word.pandoc_adapter import PandocTool
    from latex2word.pandoc_compat import PandocAssessment, PandocCompat

    monkeypatch.setattr(build_module, "locate_pandoc", lambda **_: PandocTool("/fake/pandoc", "3.12", "pandoc 3.12"))
    monkeypatch.setattr(build_module, "assess_pandoc",
                        lambda version, **_: PandocAssessment(PandocCompat.SUPPORTED, version, (), ()))
    data = {"schema_version": 1, "project": {"name": "p", "root": str(project), "main": "main.tex"},
            "profile": "generic", "workspace": {"root": str(tmp_path / "ws")}, "output": {"directory": str(tmp_path / "out")}}
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    code = main(["build", str(tmp_path / "c.yaml"), "--output", os.fspath(project / "refs.bib")])
    assert code == 2 and "L2W-E-OUTPUT-002" in capsys.readouterr().err
    assert (project / "refs.bib").read_text(encoding="utf-8") == "@book{k,title={T}}\n"
