"""The confined Pandoc reader (audit R3): flattened source + `pandoc --sandbox` reader, the default build path.

* the flattener expands what the validated graph resolved, keeps a source map, and refuses what it cannot flatten;
* the AST resource check accepts only staged, existing, relative image paths;
* the residual F-02 attacks that pass latex2word's scanner (``\\iffalse`` nesting, ``filecontents``) cannot bring the
  canary into the AST, the preliminary DOCX or the final DOCX of a default build; the unsafe legacy reader
  (``LATEX2WORD_UNSAFE_LEGACY_READER=1``) is the liveness control and still leaks a live attack;
* default builds produce the same document as the legacy single-pass reader on the equivalence fixtures.

Sandbox semantics verified with Pandoc 3.1.3 (audit container), 3.12 and 3.13 (local certification).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT
from pandockit import PANDOC, requires_pandoc
from projectkit import make_project

from latex2word.confinement import check_ast_resources, flatten, read_confined, resource_dirs
from latex2word.source_graph import SourceLocation, build_source_graph

MULTIFILE = REPO_ROOT / "tests" / "fixtures" / "audit" / "multifile_project"
CANARY = "R3CANARY77"


def _sandbox_supported() -> bool:
    if PANDOC is None:
        return False
    return subprocess.run([PANDOC, "--sandbox", "--version"], capture_output=True).returncode == 0


requires_sandbox = pytest.mark.skipif(not _sandbox_supported(), reason="pandoc with --sandbox is not installed")


# ---------------------------------------------------------------- flattening


def test_flatten_expands_resolved_inputs_includes_and_local_sty_with_a_source_map():
    graph = build_source_graph(MULTIFILE, MULTIFILE / "main.tex")
    flat = flatten(graph)
    assert "\\input{" not in flat.text and "\\include{" not in flat.text and "\\usepackage{notation}" not in flat.text
    assert "\\newcommand{\\method}{FlatSolver}" in flat.text  # local .sty inlined in the preamble
    assert flat.text.index("\\makeatletter") < flat.text.index("\\begin{document}")
    offset = flat.text.index("Nested input text")
    assert flat.locate(offset, graph) == SourceLocation("chapters/nested.tex", 1)
    assert flat.locate(flat.text.index("\\section{Results}"), graph) == SourceLocation("chapters/results.tex", 1)
    assert flat.locate(flat.text.index("\\section{Overview}"), graph).file == "main.tex"
    assert flatten(graph).text == flat.text  # deterministic


def test_flatten_keeps_unresolved_and_commented_inputs_out(tmp_path: Path):
    root = make_project(tmp_path, {"main.tex": "\\documentclass{article}\n\\begin{document}\n% \\input{gone}\nA \\input{one} B\n\\end{document}\n",
                                   "one.tex": "ONE"})
    flat = flatten(build_source_graph(root, root / "main.tex"))
    assert "A ONE\n B" in flat.text and "% \\input{gone}" in flat.text  # comment kept verbatim, not expanded


def test_flatten_splits_a_package_list_mixing_local_and_system_packages(tmp_path: Path):
    root = make_project(tmp_path, {
        "main.tex": "\\documentclass{article}\n\\usepackage[final]{amsmath, mine ,graphicx,other}\n\\begin{document}x\\end{document}\n",
        "mine.sty": "\\newcommand\\x{MINE}\n", "other.sty": "\\newcommand\\y{OTHER}\n"})
    graph = build_source_graph(root, root / "main.tex")
    text = flatten(graph).text
    preamble = text[: text.index("\\begin{document}")]
    # load order kept; system packages stay commands with their options; local ones inlined
    order = [preamble.index(s) for s in ("\\usepackage[final]{amsmath}", "\\newcommand\\x{MINE}",
                                         "\\usepackage[final]{graphicx}", "\\newcommand\\y{OTHER}")]
    assert order == sorted(order)
    assert "mine}" not in preamble and "other}" not in preamble
    assert flatten(graph).locate(text.index("\\newcommand\\y"), graph) == SourceLocation("other.sty", 1)


@requires_sandbox
def test_confined_reader_reports_a_local_package_that_was_not_inlined(tmp_path: Path):
    # the flattener inlines what the graph resolved; a project package it did not inline must not vanish silently
    # (the sandbox reports a refused \usepackage only at INFO level, together with every system package)
    root = make_project(tmp_path, {"main.tex": "\\documentclass{article}\n\\usepackage{amsmath}\n\\usepackage{late}\n"
                                               "\\begin{document}\nA \\late{} B $x$\n\\end{document}\n"})
    graph = build_source_graph(root, root / "main.tex")
    (root / "late.sty").write_text("\\newcommand\\late{LATE}\n", encoding="utf-8")  # appears after the graph was built
    work = tmp_path / "work"
    work.mkdir()
    read = read_confined(PANDOC, flatten(graph), graph, work, timeout_s=60)
    assert "--verbose" in read.argv
    assert [(f.code, f.location) for f in read.findings] == [("L2W-E-CONFINE-001", SourceLocation("main.tex", 3))]
    assert "late.sty" in read.findings[0].message  # amsmath.sty, refused as well, is a system package: no finding


@requires_sandbox
def test_default_build_finds_graphicspath_images_like_the_legacy_reader(tmp_path, monkeypatch):
    # single-pass Pandoc resolves \graphicspath in the reader; the JSON AST does not carry it, so the separate writer
    # run must get the same search path or the image is replaced by its description
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
           b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
    source = make_project(tmp_path, {
        "main.tex": "\\documentclass{article}\n\\usepackage{graphicx}\n\\graphicspath{{./figures/}}\n\\begin{document}\n"
                    "\\begin{figure}\\includegraphics{logo.png}\\caption{Logo.}\\label{fig:l}\\end{figure}\n"
                    "See Figure~\\ref{fig:l}.\n\\end{document}\n",
        "figures/logo.png": png}, name="gpath")
    legacy_run, _ = _build(tmp_path, source, "legacy", monkeypatch, legacy=True)
    default_run, ws = _build(tmp_path, source, "default", monkeypatch)
    assert legacy_run.ok and default_run.ok, ([str(f) for f in legacy_run.errors], [str(f) for f in default_run.errors])
    assert (ws / "build" / "confined_ast.json").is_file()
    media = [[n for n in zipfile.ZipFile(r.output).namelist() if n.startswith("word/media/")] for r in (legacy_run, default_run)]
    assert len(media[0]) == 1 and media[0] == media[1]
    docs = [zipfile.ZipFile(r.output).read("word/document.xml") for r in (legacy_run, default_run)]
    assert docs[0] == docs[1]


@requires_sandbox
def test_default_build_converts_a_mixed_package_list_like_the_legacy_reader(tmp_path, monkeypatch):
    source = make_project(tmp_path, {
        "main.tex": "\\documentclass{article}\n\\usepackage{amsmath,mine}\n\\begin{document}\nA \\mine{} B $\\frac{1}{2}$\n\\end{document}\n",
        "mine.sty": "\\newcommand{\\mine}{MIXEDLIST}\n"}, name="mixed")
    legacy_run, _ = _build(tmp_path, source, "legacy", monkeypatch, legacy=True)
    default_run, ws = _build(tmp_path, source, "default", monkeypatch)
    assert legacy_run.ok and default_run.ok, ([str(f) for f in legacy_run.errors], [str(f) for f in default_run.errors])
    assert (ws / "build" / "confined_ast.json").is_file()
    docs = [zipfile.ZipFile(r.output).read("word/document.xml") for r in (legacy_run, default_run)]
    assert docs[0] == docs[1] and b"MIXEDLIST" in docs[1]


# ---------------------------------------------------------------- AST resource check


def _image_ast(*targets: str) -> dict:
    return {"blocks": [{"t": "Para", "c": [{"t": "Image", "c": [["", [], []], [], [t, ""]]} for t in targets]}]}


def test_ast_resource_check(tmp_path: Path):
    staging = tmp_path / "staging"
    (staging / "figures").mkdir(parents=True)
    (staging / "figures" / "a.png").write_bytes(b"png")
    (tmp_path / "outside.png").write_bytes(b"png")
    ast = _image_ast("figures/a.png", "/etc/passwd", "C:/x.png", "https://example.org/x.png", "../outside.png", "figures/none.png")
    problems = [f.message for f in check_ast_resources(ast, staging, staging)]
    assert len(problems) == 5
    assert any("absolute" in p and "/etc/passwd" in p for p in problems)
    assert any("URL" in p for p in problems) and any("outside the staged project" in p for p in problems)
    assert any("does not exist" in p for p in problems)


def test_ast_resource_check_follows_the_writer_search_path_and_stays_confined(tmp_path: Path):
    staging = tmp_path / "staging"
    (staging / "figures").mkdir(parents=True)
    (staging / "figures" / "logo.png").write_bytes(b"png")
    (tmp_path / "escape").mkdir()
    (tmp_path / "escape" / "out.png").write_bytes(b"png")
    ast = _image_ast("logo.png", "out.png")
    assert len(check_ast_resources(ast, staging, staging)) == 2  # without the search path neither is found
    problems = [f.message for f in check_ast_resources(ast, staging, staging, [staging, staging / "figures", tmp_path / "escape"])]
    assert len(problems) == 1 and "out.png" in problems[0] and "outside the staged project" in problems[0]


def test_resource_dirs_are_the_main_directory_then_the_graphicspath(tmp_path: Path):
    root = make_project(tmp_path, {"main.tex": "\\documentclass{article}\n\\usepackage{graphicx}\n"
                                               "\\graphicspath{{./figures/}{img/}}\n\\begin{document}x\\end{document}\n"})
    graph = build_source_graph(root, root / "main.tex")
    assert resource_dirs(graph, root) == [root, root / "figures", root / "img"]


# ---------------------------------------------------------------- residual attacks


ATTACKS = {
    "iffalse-nesting": "Hello \\iffalse \\ifx \\fi \\input{@C@} \\fi end.",
    "filecontents": "\\begin{filecontents}{x.tex}\n\\input{@C@}\n\\end{filecontents}\nHi.",
}


def _attack_is_live(project: Path) -> bool:
    """Control: does the UNCONFINED reader read the canary? Whether an attack is live depends on the Pandoc version
    (``\\iffalse`` nesting reads the canary with Pandoc 3.1.3, not with 3.12/3.13)."""
    plain = subprocess.run([PANDOC, "-f", "latex", "-t", "json", "main.tex"], cwd=project, capture_output=True,
                           text=True, encoding="utf-8", errors="replace").stdout
    return CANARY in plain


@requires_sandbox
@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_confined_reader_keeps_the_residual_attacks_out_of_the_ast(tmp_path: Path, attack: str):
    secret = tmp_path / "outside" / "canary.tex"
    secret.parent.mkdir()
    secret.write_text(CANARY + "\n", encoding="utf-8")
    body = ATTACKS[attack].replace("@C@", secret.with_suffix("").as_posix())
    root = make_project(tmp_path, {"main.tex": f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"})
    graph = build_source_graph(root, root / "main.tex")
    live = _attack_is_live(root)
    work = tmp_path / "work"
    work.mkdir()
    read = read_confined(PANDOC, flatten(graph), graph, work, timeout_s=60)
    assert read.ast is not None and CANARY not in json.dumps(read.ast)  # whatever this Pandoc does
    if not live:  # this Pandoc does not follow the construct at all: nothing to refuse
        assert read.findings == []
        return
    assert [f.code for f in read.findings] == ["L2W-E-CONFINE-001"]
    assert read.findings[0].location == SourceLocation("main.tex", 3 if attack == "iffalse-nesting" else 4)


# ---------------------------------------------------------------- end to end: default (confined) vs unsafe legacy reader

LEGACY_ENV = "LATEX2WORD_UNSAFE_LEGACY_READER"


def _build(tmp_path: Path, source: Path, tag: str, monkeypatch: pytest.MonkeyPatch, legacy: bool = False):
    import latex2word.build as build_module
    import latex2word.ooxml.validation as validation
    from latex2word.config import load_config
    from latex2word.pandoc_compat import PandocAssessment, PandocCompat

    # Pandoc 3.1.3 (audit container): bypass the compatibility gate and the schema-order check, as test_audit_e2e does
    monkeypatch.setattr(validation, "_check_schema_order", lambda roots, result: None)
    monkeypatch.setattr(build_module, "assess_pandoc",
                        lambda version, **_: PandocAssessment(PandocCompat.TESTED_UNCERTIFIED, version or "?", (), ("test",)))
    monkeypatch.delenv("LATEX2WORD_EXPERIMENTAL_CONFINED_READER", raising=False)  # retired: no effect either way
    if legacy:
        monkeypatch.setenv(LEGACY_ENV, "1")
    else:
        monkeypatch.delenv(LEGACY_ENV, raising=False)
    project = tmp_path / f"project-{tag}"
    shutil.copytree(source, project)
    data = {"schema_version": 1, "project": {"name": tag, "root": str(project), "main": "main.tex"}, "profile": "generic",
            "workspace": {"root": str(tmp_path / f"ws-{tag}")}, "output": {"directory": str(tmp_path / f"out-{tag}")}}
    (tmp_path / f"{tag}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return build_module.run_build(load_config(tmp_path / f"{tag}.yaml")), tmp_path / f"ws-{tag}"


@requires_sandbox
def test_default_build_gives_the_same_document_as_the_legacy_reader_on_the_multifile_fixture(tmp_path, monkeypatch):
    legacy_run, _ = _build(tmp_path, MULTIFILE, "legacy", monkeypatch, legacy=True)
    default_run, ws = _build(tmp_path, MULTIFILE, "default", monkeypatch)
    assert legacy_run.ok and default_run.ok, ([str(f) for f in legacy_run.errors], [str(f) for f in default_run.errors])
    assert (ws / "build" / "confined_ast.json").is_file()  # the confined path really ran
    docs = [zipfile.ZipFile(r.output).read("word/document.xml") for r in (legacy_run, default_run)]
    assert hashlib.sha256(docs[0]).digest() == hashlib.sha256(docs[1]).digest()
    text = docs[1].decode("utf-8")
    for expected in ("FlatSolver", "Nested input text", "Knuth", "Lamport", "50%"):
        assert expected in text, expected
    assert len([n for n in zipfile.ZipFile(default_run.output).namelist() if n.startswith("word/media/")]) == 1


@requires_sandbox
@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_default_build_blocks_the_residual_attacks_end_to_end(tmp_path, monkeypatch, attack: str):
    secret = tmp_path / "outside" / "canary.tex"
    secret.parent.mkdir()
    secret.write_text(CANARY + "\n", encoding="utf-8")
    source = tmp_path / "src"
    make_project(tmp_path, {"main.tex": "\\documentclass{article}\n\\begin{document}\n"
                            + ATTACKS[attack].replace("@C@", secret.with_suffix("").as_posix()) + "\n\\end{document}\n"},
                 name="src")
    live = _attack_is_live(source)
    result, ws = _build(tmp_path, source, attack, monkeypatch)
    for path in [*ws.rglob("*"), *(tmp_path / f"out-{attack}").rglob("*")]:
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes(), path
    if not live:  # not followed by this Pandoc: the build converts the visible text and nothing else
        assert result.ok, [str(f) for f in result.errors]
        return
    assert not result.ok and "L2W-E-CONFINE-001" in {f.code for f in result.errors}
    assert result.output is None and not (ws / "build" / "preliminary.docx").exists()


@requires_sandbox
@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_the_unsafe_legacy_reader_is_the_liveness_control(tmp_path, monkeypatch, attack: str):
    # the vulnerability the default closes, demonstrated only here: a live attack leaks through the legacy reader
    secret = tmp_path / "outside" / "canary.tex"
    secret.parent.mkdir()
    secret.write_text(CANARY + "\n", encoding="utf-8")
    source = tmp_path / "src"
    make_project(tmp_path, {"main.tex": "\\documentclass{article}\n\\begin{document}\n"
                            + ATTACKS[attack].replace("@C@", secret.with_suffix("").as_posix()) + "\n\\end{document}\n"},
                 name="src")
    live = _attack_is_live(source)
    result, ws = _build(tmp_path, source, attack, monkeypatch, legacy=True)
    assert "L2W-W-CONFINE-005" in {f.code for f in result.findings}
    leaked = CANARY.encode() in (ws / "build" / "preliminary.docx").read_bytes() or (
        result.output is not None and CANARY.encode() in zipfile.ZipFile(result.output).read("word/document.xml"))
    # live with this Pandoc (filecontents on 3.12/3.13): the legacy reader leaks; not live (iffalse nesting on
    # 3.12/3.13): it does not follow the construct either
    assert leaked == live


# ---------------------------------------------------------------- reader selection


@requires_sandbox
def test_a_default_build_uses_the_confined_reader_without_any_setting(tmp_path, monkeypatch):
    result, ws = _build(tmp_path, MULTIFILE, "default", monkeypatch)
    assert result.ok, [str(f) for f in result.errors]
    assert (ws / "build" / "confined_ast.json").is_file()
    run = json.loads((ws / "build" / "pandoc_run.json").read_text(encoding="utf-8"))
    assert "json" in run["argv"] and "--sandbox" not in run["argv"]  # the writer converts the confined AST
    assert not any(f.code.startswith("L2W-W-CONFINE") for f in result.findings)


@requires_sandbox
def test_the_retired_experimental_variable_cannot_switch_confinement_off(tmp_path, monkeypatch):
    result, ws = _build(tmp_path, MULTIFILE, "retired", monkeypatch)
    monkeypatch.setenv("LATEX2WORD_EXPERIMENTAL_CONFINED_READER", "0")  # whatever value: it is not read any more
    import latex2word.build as build_module
    from latex2word.config import load_config

    again = build_module.run_build(load_config(tmp_path / "retired.yaml"))
    assert result.ok and again.ok
    assert (ws / "build" / "confined_ast.json").is_file()
    assert not any(f.code == "L2W-W-CONFINE-005" for f in again.findings)


@requires_sandbox
def test_the_unsafe_legacy_reader_is_explicit_and_warned(tmp_path, monkeypatch):
    result, ws = _build(tmp_path, MULTIFILE, "legacy", monkeypatch, legacy=True)
    assert result.ok, [str(f) for f in result.errors]
    warnings = [f for f in result.findings if f.code == "L2W-W-CONFINE-005"]
    assert len(warnings) == 1 and "unconfined" in warnings[0].message
    assert not (ws / "build" / "confined_ast.json").exists()


def test_the_profile_arguments_reach_the_confined_reader(tmp_path: Path):
    from latex2word.process import CommandResult

    seen: list[list[str]] = []

    def runner(argv, **_):
        seen.append(list(argv))
        return CommandResult(tuple(argv), 0, '{"pandoc-api-version":[1,23],"meta":{},"blocks":[]}', "", False, 0.0)

    root = make_project(tmp_path, {"main.tex": "\\documentclass{article}\n\\begin{document}\nx\n\\end{document}\n"})
    graph = build_source_graph(root, root / "main.tex")
    work = tmp_path / "work"
    work.mkdir()
    read = read_confined("pandoc", flatten(graph), graph, work, timeout_s=5, runner=runner,
                         extra_args=["--metadata=lang:es-ES"])
    assert read.ast is not None and read.findings == []
    argv = seen[0]
    assert argv[:3] == ["pandoc", "--sandbox", "--verbose"]
    assert argv.index("--metadata=lang:es-ES") < argv.index("l2w-flattened.tex") == len(argv) - 1
