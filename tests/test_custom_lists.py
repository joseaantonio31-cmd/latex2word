"""tocloft custom lists (\\newlistof): \\listof<name> -> TOC \\f field; \\addcontentsline{ext} entries -> TC fields."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import yaml
from conftest import REPO_ROOT
from pandockit import requires_pandoc

from latex2word.diagnostics import Severity

MAIN = r"""\documentclass{report}
\usepackage[spanish]{babel}
\usepackage{tocloft}
\newcommand{\listextraname}{Índice de extras}
\newlistof{extra}{lxe}{\listextraname}
\newcommand{\registrarextra}[1]{%
    \addcontentsline{lxe}{extra}{\protect\numberline{\thechapter}#1}%
}
\begin{document}
\listofextra
\addcontentsline{toc}{chapter}{Entrada manual del índice general}
\chapter{Uno}
Texto.
\appendix
\chapter{Primer extra}
\registrarextra{Primer extra con "comillas" y <signos>}
Texto.
\chapter{Segundo extra}
\registrarextra{Segundo extra}
Texto.
\end{document}
"""


@requires_pandoc
def test_custom_list_entries_become_tc_fields_collected_by_a_toc_field(tmp_path: Path):
    from latex2word.build import run_build
    from latex2word.config import load_config

    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tex").write_text(MAIN, encoding="utf-8")
    profiles = REPO_ROOT / "tests" / "fixtures" / "semantic" / "report_structure" / "profiles"
    config = {
        "schema_version": 1,
        "project": {"name": "lists", "root": str(project), "main": "main.tex"},
        "profile": "report-es-test",
        "profile_dirs": [str(profiles)],
        "workspace": {"root": str(tmp_path / "workspace")},
        "output": {"directory": str(tmp_path / "out")},
        "tools": {"timeout_s": 120},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    result = run_build(load_config(path))
    errors = [str(f) for f in result.findings if f.severity is Severity.ERROR]
    assert result.ok and not errors, errors
    document = zipfile.ZipFile(result.output).read("word/document.xml").decode("utf-8")
    instructions = [i.strip() for i in re.findall(r"<w:instrText[^>]*>([^<]*)</w:instrText>", document)]
    # the list: its resolved title as a heading, then a TOC field over identifier L
    assert "Índice de extras" in document
    assert 'TOC \\h \\z \\f L' in instructions
    # one TC entry per \registrarextra, numbered with the appendix letter (\thechapter), quotes and markup escaped
    tc = [i for i in instructions if i.startswith("TC ")]
    assert tc == [
        # Stage 3: a manual \addcontentsline{toc}{chapter}{..} is a bare TC entry of the main TOC (level of a chapter)
        'TC "Entrada manual del índice general" \\l 1',
        'TC "A  Primer extra con \'comillas\' y &lt;signos&gt;" \\f L \\l 1',
        'TC "B  Segundo extra" \\f L \\l 1',
    ]
    # field codes only: no entry or marker text is visible in the document
    visible = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", document))
    assert "Entrada manual" not in visible and "l2wcontents" not in document and "lxe" not in visible
