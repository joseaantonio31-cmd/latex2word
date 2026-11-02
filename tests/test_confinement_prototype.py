"""R3 prototype (EXPERIMENTAL): flattened source + `pandoc --sandbox` reader (reports/audit/remediation-phase2.md).

The prototype is not the production path; these tests pin its evidence:

* the flattener expands what the validated graph resolved, keeps a source map, and refuses what it cannot flatten;
* the AST resource check accepts only staged, existing, relative image paths;
* the two residual F-02 attacks that pass latex2word's scanner (``\\iffalse`` nesting, ``filecontents``) cannot bring
  the canary into the AST, the preliminary DOCX or the final DOCX;
* on the multi-file fixture the experimental path yields the same final document.xml as production.

Executed with Pandoc 3.1.3 in the audit container; sandbox semantics must be re-checked with Pandoc 3.12/3.13 locally.
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

from latex2word.confinement import FlattenError, check_ast_resources, flatten, read_confined
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


def test_flatten_refuses_a_package_list_mixing_local_and_system_packages(tmp_path: Path):
    root = make_project(tmp_path, {"main.tex": "\\documentclass{article}\n\\usepackage{amsmath,mine}\n\\begin{document}x\\end{document}\n",
                                   "mine.sty": "\\newcommand\\x{y}\n"})
    with pytest.raises(FlattenError, match="mixes a local package"):
        flatten(build_source_graph(root, root / "main.tex"))


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


# ---------------------------------------------------------------- residual attacks


ATTACKS = {
    "iffalse-nesting": "Hello \\iffalse \\ifx \\fi \\input{@C@} \\fi end.",
    "filecontents": "\\begin{filecontents}{x.tex}\n\\input{@C@}\n\\end{filecontents}\nHi.",
}


@requires_sandbox
@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_confined_reader_keeps_the_residual_attacks_out_of_the_ast(tmp_path: Path, attack: str):
    secret = tmp_path / "outside" / "canary.tex"
    secret.parent.mkdir()
    secret.write_text(CANARY + "\n", encoding="utf-8")
    body = ATTACKS[attack].replace("@C@", secret.with_suffix("").as_posix())
    root = make_project(tmp_path, {"main.tex": f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"})
    graph = build_source_graph(root, root / "main.tex")
    # control: the unconfined reader does read the canary (the attack is real with this Pandoc)
    plain = subprocess.run([PANDOC, "-f", "latex", "-t", "json", "main.tex"], cwd=root, capture_output=True, text=True).stdout
    assert CANARY in plain
    work = tmp_path / "work"
    work.mkdir()
    read = read_confined(PANDOC, flatten(graph), graph, work, timeout_s=60)
    assert read.ast is not None and CANARY not in json.dumps(read.ast)
    assert [f.code for f in read.findings] == ["L2W-E-CONFINE-001"]
    assert read.findings[0].location == SourceLocation("main.tex", 3 if attack == "iffalse-nesting" else 4)


# ---------------------------------------------------------------- end to end (experimental switch)


def _build(tmp_path: Path, source: Path, tag: str, monkeypatch: pytest.MonkeyPatch, experimental: bool):
    import latex2word.build as build_module
    import latex2word.ooxml.validation as validation
    from latex2word.config import load_config
    from latex2word.pandoc_compat import PandocAssessment, PandocCompat

    # Pandoc 3.1.3 (audit container): bypass the compatibility gate and the schema-order check, as test_audit_e2e does
    monkeypatch.setattr(validation, "_check_schema_order", lambda roots, result: None)
    monkeypatch.setattr(build_module, "assess_pandoc",
                        lambda version, **_: PandocAssessment(PandocCompat.TESTED_UNCERTIFIED, version or "?", (), ("test",)))
    if experimental:
        monkeypatch.setenv("LATEX2WORD_EXPERIMENTAL_CONFINED_READER", "1")
    else:
        monkeypatch.delenv("LATEX2WORD_EXPERIMENTAL_CONFINED_READER", raising=False)
    project = tmp_path / f"project-{tag}"
    shutil.copytree(source, project)
    data = {"schema_version": 1, "project": {"name": tag, "root": str(project), "main": "main.tex"}, "profile": "generic",
            "workspace": {"root": str(tmp_path / f"ws-{tag}")}, "output": {"directory": str(tmp_path / f"out-{tag}")}}
    (tmp_path / f"{tag}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return build_module.run_build(load_config(tmp_path / f"{tag}.yaml")), tmp_path / f"ws-{tag}"


@requires_sandbox
def test_experimental_path_gives_the_same_document_as_production_on_the_multifile_fixture(tmp_path, monkeypatch):
    prod, _ = _build(tmp_path, MULTIFILE, "prod", monkeypatch, experimental=False)
    exp, ws = _build(tmp_path, MULTIFILE, "exp", monkeypatch, experimental=True)
    assert prod.ok and exp.ok, ([str(f) for f in prod.errors], [str(f) for f in exp.errors])
    assert (ws / "build" / "confined_ast.json").is_file()  # the experimental path really ran
    docs = [zipfile.ZipFile(r.output).read("word/document.xml") for r in (prod, exp)]
    assert hashlib.sha256(docs[0]).digest() == hashlib.sha256(docs[1]).digest()
    text = docs[1].decode("utf-8")
    for expected in ("FlatSolver", "Nested input text", "Knuth", "Lamport", "50%"):
        assert expected in text, expected
    assert len([n for n in zipfile.ZipFile(exp.output).namelist() if n.startswith("word/media/")]) == 1


@requires_sandbox
@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_experimental_build_blocks_the_residual_attacks_end_to_end(tmp_path, monkeypatch, attack: str):
    secret = tmp_path / "outside" / "canary.tex"
    secret.parent.mkdir()
    secret.write_text(CANARY + "\n", encoding="utf-8")
    source = tmp_path / "src"
    make_project(tmp_path, {"main.tex": "\\documentclass{article}\n\\begin{document}\n"
                            + ATTACKS[attack].replace("@C@", secret.with_suffix("").as_posix()) + "\n\\end{document}\n"},
                 name="src")
    result, ws = _build(tmp_path, source, attack, monkeypatch, experimental=True)
    assert not result.ok and "L2W-E-CONFINE-001" in {f.code for f in result.errors}
    assert result.output is None and not (ws / "build" / "preliminary.docx").exists()
    for path in ws.rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes(), path
