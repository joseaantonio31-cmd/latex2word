"""No zero-width-space bases in OMML: base-less plain scripts become text scripts; siunitx degrees need no script."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import yaml
from pandockit import requires_pandoc

from latex2word.diagnostics import Severity

MAIN = (
    "\\documentclass{report}\\usepackage{amsmath}\\usepackage{siunitx}\\begin{document}\n"
    "Intervalo IC$_{95}$ y IC$_{\\mathrm{95}}$, valor 0.13$^{*}$ y nota $^{*}$: marcada.\n"
    "Mínimo $T_{\\min}$ y $x_{1}^{2}$; daga $^{\\dagger}$; temperatura $\\SI{25}{\\celsius}$ y $\\ang{30}$.\n"
    "\\end{document}\n"
)


def _build(tmp_path: Path):
    from latex2word.build import run_build
    from latex2word.config import load_config

    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tex").write_text(MAIN, encoding="utf-8")
    config = {
        "schema_version": 1,
        "project": {"name": "zw", "root": str(project), "main": "main.tex"},
        "profile": "generic",
        "workspace": {"root": str(tmp_path / "workspace")},
        "output": {"directory": str(tmp_path / "out")},
        "tools": {"timeout_s": 120},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return run_build(load_config(path))


@requires_pandoc
def test_no_zero_width_math_bases_and_plain_scripts_become_text_scripts(tmp_path: Path):
    result = _build(tmp_path)
    errors = [str(f) for f in result.findings if f.severity is Severity.ERROR]
    assert result.ok and not errors, errors
    document = zipfile.ZipFile(result.output).read("word/document.xml").decode("utf-8")
    math_runs = re.findall(r"<m:t[^>]*>([^<]*)</m:t>", document)
    assert not any("\u200b" in t for t in math_runs)  # no invented bases inside OMML
    # IC$_{95}$, IC$_{\mathrm{95}}$ -> text subscripts; $^{*}$ -> text superscripts
    assert len(re.findall(r'<w:vertAlign w:val="subscript"/></w:rPr><w:t[^>]*>95</w:t>', document)) == 2
    assert len(re.findall(r'<w:vertAlign w:val="superscript"/></w:rPr><w:t[^>]*>\*</w:t>', document)) == 2
    # a mark command with a Unicode equivalent becomes a text superscript too; scripts with a base stay math
    assert re.search(r'<w:vertAlign w:val="superscript"/></w:rPr><w:t[^>]*>\u2020</w:t>', document)
    assert "<m:sSub>" in document and "<m:sSubSup>" in document  # T_{\min}, x_1^2
    # siunitx degrees in math: a plain character, no superscript construction
    assert any("\u00b0C" in t or t == "\u00b0" for t in math_runs) or "\u00b0C" in "".join(math_runs)
