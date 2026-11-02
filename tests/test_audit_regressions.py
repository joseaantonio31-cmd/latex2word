"""Regression tests for the defects found by the 2026-11 engineering audit (reports/audit/findings.md).

Each test names its finding (F-xx). The oracles are independent of the code under test where that is feasible:

* math (F-01, F-03): the OMML that Pandoc writes is linearised here, including delimiter characters
  (``m:d``/``m:begChr``), and compared with the expected mathematical content -- not with the filter's own output;
  the filter module is additionally exercised directly through ``pandoc lua`` with Pandoc-3.12-shaped math strings,
  so the tests do not depend on how a particular Pandoc release wraps ``\\begin{equation}``;
* containment (F-02): the source graph must report an ERROR (which stops the build before Pandoc runs) for every
  construct that Pandoc was observed to open by itself from an arbitrary path;
* reference loss (F-04): ``build._lost_references`` is fed a hand-built scan and filter report.
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from lxml import etree
from pandockit import PANDOC, document, requires_pandoc, run_filter
from projectkit import make_project

from latex2word.diagnostics import Severity
from latex2word.labels import LabelReference, LabelScan
from latex2word.source_graph import SourceLocation, build_source_graph

FILTER_DIR = Path(__file__).resolve().parents[1] / "src" / "latex2word" / "filters"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


# ---------------------------------------------------------------- independent OMML linearisation


def _linear(element: etree._Element) -> str:
    tag = etree.QName(element).localname
    if tag == "t":
        return element.text or ""
    if tag == "d":  # delimiter: default characters are ( and )
        properties = element.find(f"{{{M}}}dPr")
        begin = properties.find(f"{{{M}}}begChr") if properties is not None else None
        end = properties.find(f"{{{M}}}endChr") if properties is not None else None
        inner = "".join(_linear(c) for c in element if etree.QName(c).localname == "e")
        return (begin.get(f"{{{M}}}val") if begin is not None else "(") + inner + (
            end.get(f"{{{M}}}val") if end is not None else ")"
        )
    return "".join(_linear(c) for c in element if isinstance(c.tag, str))


def omml_lines(tmp_path: Path, body: str) -> tuple[list[str], dict]:
    path, report = run_filter(tmp_path, document(body), to="docx", output="out.docx")
    root = etree.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    paras = list(root.iter(f"{{{M}}}oMathPara"))
    return ["".join(_linear(p) for p in [para]).replace("\u200b", "").replace(" ", "") for para in paras], report


# ---------------------------------------------------------------- F-01: "\\ [x,y]" row content was deleted


@requires_pandoc
def test_f01_bracket_starting_an_aligned_row_survives_in_the_omml(tmp_path: Path):
    lines, report = omml_lines(tmp_path, "\\[\\begin{aligned} a &= b \\\\ [x,y] &= z \\end{aligned}\\]")
    assert lines == ["a=b[x,y]=z"], lines
    assert report["diagnostics"] == []


@requires_pandoc
def test_f01_spacing_argument_directly_after_the_row_break_is_still_spacing(tmp_path: Path):
    lines, _ = omml_lines(tmp_path, "\\[\\begin{aligned} a &= b \\\\[2pt] c &= d \\end{aligned}\\]")
    assert lines == ["a=bc=d"], lines


@requires_pandoc
def test_f01_bracket_after_starred_row_break_survives(tmp_path: Path):
    lines, _ = omml_lines(tmp_path, "\\[\\begin{aligned} a &= b \\\\* [c] &= d \\end{aligned}\\]")
    assert lines == ["a=b[c]=d"], lines


@requires_pandoc
def test_f01_alternating_user_array_keeps_kernel_semantics(tmp_path: Path):
    # in a kernel array, "\\ [2pt]" IS spacing (\@ifnextchar skips spaces): it must not become content
    lines, _ = omml_lines(tmp_path, "\\[\\begin{array}{rl} a &= b \\\\ [2pt] c &= d \\end{array}\\]")
    assert lines == ["a=bc=d"], lines


# The math module itself, with the math strings Pandoc 3.12 produces (wrapper and label inside the string).
_LUA_PROBE = r"""
package.path = '%(dir)s/?.lua;' .. package.path
local M = require('l2w_math')
local cases = %(cases)s
local out = {}
for i, c in ipairs(cases) do
  local r = M.analyze(c, true)
  local codes = {}
  for _, e in ipairs(r.errors) do codes[#codes + 1] = e.code end
  out[i] = { tex = r.tex, numbered = r.numbered, labels = r.labels, codes = codes }
end
print(pandoc.json.encode(out))
"""


def _lua_analyze(tmp_path: Path, cases: list[str]) -> list[dict]:
    lua_list = "{" + ", ".join("[==[" + c + "]==]" for c in cases) + "}"
    script = tmp_path / "probe.lua"
    script.write_text(_LUA_PROBE % {"dir": FILTER_DIR.as_posix(), "cases": lua_list}, encoding="utf-8")
    completed = subprocess.run([PANDOC, "lua", str(script)], capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0 and "lua" in completed.stderr.lower() and "unknown" in completed.stderr.lower():
        pytest.skip("this Pandoc has no `pandoc lua` subcommand")
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    for entry in result:  # Lua encodes empty tables as {}
        for key in ("labels", "codes"):
            if isinstance(entry[key], dict):
                entry[key] = list(entry[key].values())
    return result


@requires_pandoc
def test_f01_numbered_align_keeps_the_bracket_row_and_its_number(tmp_path: Path):
    [result] = _lua_analyze(
        tmp_path, ["\\begin{align} a &= b \\nonumber \\\\ [x,y] &= z \\label{eq:r} \\end{align}"]
    )
    assert "[x,y]" in result["tex"]
    assert result["numbered"] is True and result["labels"] == ["eq:r"] and result["codes"] == []


@requires_pandoc
def test_f01_eqnarray_is_a_kernel_environment_and_keeps_its_spacing_semantics(tmp_path: Path):
    [result] = _lua_analyze(tmp_path, ["\\begin{eqnarray*} a &=& b \\\\ [2pt] c &=& d \\end{eqnarray*}"])
    assert "2pt" not in result["tex"] and "c" in result["tex"]


# ---------------------------------------------------------------- F-03: label with nested braces


@requires_pandoc
def test_f03_label_with_nested_braces_is_removed_from_the_math_and_registered(tmp_path: Path):
    [result] = _lua_analyze(tmp_path, ["\\begin{equation} x = 1 \\label{eq:a_{1}} \\end{equation}"])
    assert "\\label" not in result["tex"]
    assert result["labels"] == ["eq:a_{1}"] and result["numbered"] is True


# ---------------------------------------------------------------- F-02: reads outside the project root


def _graph(tmp_path: Path, body: str, preamble: str = ""):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.tex").write_text("SECRET\n", encoding="utf-8")
    secret = (outside / "secret").as_posix()
    tex = (
        "\\documentclass{article}\n\\usepackage{graphicx}\n"
        + preamble.replace("@S@", secret)
        + "\n\\begin{document}\n"
        + body.replace("@S@", secret)
        + "\n\\end{document}\n"
    )
    root = make_project(tmp_path, {"main.tex": tex})
    return build_source_graph(root, root / "main.tex")


@pytest.mark.parametrize(
    ("name", "preamble", "body", "code"),
    [
        ("macro-input", "\\newcommand{\\p}{@S@}", "\\input{\\p}", "dynamic-path"),
        ("macro-include", "\\newcommand{\\p}{@S@}", "\\include{\\p}", "dynamic-path"),
        ("macro-graphic", "\\newcommand{\\p}{@S@.png}", "\\includegraphics{\\p}", "dynamic-path"),
        ("wrapper-macro", "\\newcommand{\\inp}[1]{\\input{#1}}", "\\inp{@S@}", "dynamic-path"),
        ("let-alias-input", "\\let\\inp\\input", "\\inp{@S@}", "unsupported-syntax"),
        ("let-alias-graphic", "\\let\\ig\\includegraphics", "\\ig{@S@.png}", "unsupported-syntax"),
        ("lstinputlisting", "", "\\lstinputlisting{@S@.tex}", "unsupported-command"),
        ("inputminted", "", "\\inputminted{tex}{@S@.tex}", "unsupported-command"),
        ("subfile", "", "\\subfile{@S@.tex}", "unsupported-command"),
    ],
)
def test_f02_constructs_pandoc_opens_from_any_path_are_errors(tmp_path: Path, name: str, preamble: str, body: str, code: str):
    graph = _graph(tmp_path, body, preamble)
    errors = [f for f in graph.findings if f.severity is Severity.ERROR]
    assert any(f.code == code for f in errors), [(f.code, f.severity, f.message) for f in graph.findings]
    assert graph.has_errors


def test_f02_literal_paths_outside_the_root_were_already_errors(tmp_path: Path):
    graph = _graph(tmp_path, "\\include{@S@}")
    assert graph.has_errors and any(f.code == "outside-root" for f in graph.findings)


def test_f02_commands_pandoc_does_not_open_stay_warnings(tmp_path: Path):
    # observed with Pandoc 3.1.3: \verbatiminput is not read by the reader, so no containment problem
    graph = _graph(tmp_path, "\\verbatiminput{@S@.tex}")
    finding = next(f for f in graph.findings if f.code == "unsupported-command")
    assert finding.severity is Severity.WARNING


# ---------------------------------------------------------------- F-04: reference dropped by the reader


def _lost_references(*args):
    from latex2word.build import _lost_references as lost  # absent before the fix: imported per test

    return lost(*args)


def _scan(*refs: tuple[str, str]) -> LabelScan:
    return LabelScan((), tuple(LabelReference(label, command, SourceLocation("main.tex", 3)) for command, label in refs))


def test_f04_reference_absent_from_the_filter_report_is_an_error():
    findings = _lost_references(_scan(("Cref", "tab:a"), ("ref", "sec:b")), {"refs": [{"label": "sec:b"}]})
    assert [(f.code, f.severity) for f in findings] == [("L2W-E-REF-012", Severity.ERROR)]
    assert "tab:a" in findings[0].message and findings[0].location == SourceLocation("main.tex", 3)


def test_f04_every_reference_present_or_reported_elsewhere_gives_no_finding():
    report = {"refs": [{"label": "tab:a"}, {"label": "missing", "resolved": False}]}
    assert _lost_references(_scan(("Cref", "tab:a"), ("ref", "missing"), ("pageref", "tab:z")), report) == []


def test_f04_source_location_is_preferred_over_the_staged_one():
    staged = LabelScan((), (LabelReference("x", "ref", SourceLocation("main.tex", 40)),))
    source = LabelScan((), (LabelReference("x", "ref", SourceLocation("main.tex", 12)),))
    [finding] = _lost_references(staged, {"refs": []}, source)
    assert finding.location == SourceLocation("main.tex", 12)


# ---------------------------------------------------------------- F-05: build ignored the Pandoc compatibility gate


def _gate_project(tmp_path: Path):
    import yaml

    from latex2word.config import load_config

    root = make_project(tmp_path, {"main.tex": "\\documentclass{article}\n\\begin{document}\nText.\n\\end{document}\n"})
    data = {
        "schema_version": 1,
        "project": {"name": "gate", "root": str(root), "main": "main.tex"},
        "profile": "generic",
        "workspace": {"root": str(tmp_path / "ws")},
        "output": {"directory": str(tmp_path / "out")},
    }
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(tmp_path / "c.yaml")


@pytest.mark.parametrize(
    ("compat", "code", "blocks"),
    [("UNSUPPORTED", "L2W-E-PANDOC-003", True), ("TESTED_UNCERTIFIED", "L2W-W-PANDOC-003", False)],
)
def test_f05_build_applies_the_pandoc_classification_that_doctor_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compat: str, code: str, blocks: bool
):
    import latex2word.build as build_module
    from latex2word.pandoc_adapter import PandocTool
    from latex2word.pandoc_compat import PandocAssessment, PandocCompat

    seen: dict[str, object] = {}

    def assess(version, **kwargs):
        seen.update(kwargs, version=version)
        return PandocAssessment(PandocCompat[compat], version, (), ("reader AST differs",))

    monkeypatch.setattr(build_module, "locate_pandoc", lambda **_: PandocTool("/fake/pandoc", "3.1.3", "pandoc 3.1.3"))
    monkeypatch.setattr(build_module, "assess_pandoc", assess)
    if not blocks:  # stop right after the gate: the rest of the pipeline is not under test here
        monkeypatch.setattr(build_module, "run_figures", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("past gate")))
    config = _gate_project(tmp_path)
    if blocks:
        from latex2word.errors import BuildError, ExitCode

        with pytest.raises(BuildError, match=code) as raised:
            build_module.run_build(config)
        assert raised.value.exit_code is ExitCode.ERROR  # tool problem: exit code 2, like an unusable Pandoc
        assert not (tmp_path / "ws").exists()  # stopped before any workspace was created
    else:
        with pytest.raises(RuntimeError, match="past gate"):
            build_module.run_build(config)
    assert seen["probe"] is True and seen["path"] == "/fake/pandoc" and seen["version"] == "3.1.3"
