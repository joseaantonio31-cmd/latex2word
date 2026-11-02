"""Shared helpers for the layout tests: generate fictional LaTeX projects, build them, inspect the final DOCX."""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from conftest import REPO_ROOT
from lxml import etree

from latex2word.build import BuildResult, run_build
from latex2word.config import load_config
from latex2word.ooxml.namespaces import NS, qn, text_of, w

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "semantic"
GENERIC_ARTICLE = FIXTURES / "generic_article"

PREAMBLE = r"""\documentclass{article}
\usepackage{amsmath}
\usepackage{graphicx}
\usepackage{booktabs}
"""
BIB_TAIL = r"""
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""

DEFAULT_AUTHORS = (
    r"\author{Ada Example\\Department of Fictional Studies\\Example University\\ada@example.org"
    r" \and Zo\"e \"Ornek\\Institute of Samples\\Sample City\\zoe@example.org}"
)


IEEE_AUTHORS = (
    "\\author{\\IEEEauthorblockN{Ada Example}\n\\IEEEauthorblockA{Department of Fictional Studies\\\\Example University\\\\ada@example.org}\n"
    "\\and\n\\IEEEauthorblockN{Zo\\\"e \\\"Ornek}\n\\IEEEauthorblockA{Institute of Samples\\\\Sample City\\\\zoe@example.org}}"
)
IEEE_KEYWORDS = "\\begin{IEEEkeywords}layout, fixtures, generic\\end{IEEEkeywords}"


def article(
    *,
    author: str = DEFAULT_AUTHORS,
    figures: tuple[str, ...] = ("0.98\\columnwidth",),
    equations: str | None = None,
    keywords: str = "\\keywords{layout, fixtures, generic}",
) -> str:
    """A fictional article exercising front matter, a figure per width, a table, equations and citations."""
    body = [
        PREAMBLE,
        "\\title{A Fictional Layout Study}",
        author,
        "\\begin{document}\n\\maketitle",
        "\\begin{abstract}\nA fictional abstract with inline math $x^2 + y^2$ and a cited result \\cite{alpha}.\n\\end{abstract}",
        keywords,
        "\\section{Introduction}\\label{sec:intro}",
        "Prior work \\cite{beta} motivates Fig.~\\ref{fig:f0} and Table~\\ref{tab:one}.",
    ]
    for index, width in enumerate(figures):
        body.append(
            f"\\begin{{figure}}[t]\n\\centering\\includegraphics[width={width}]{{figures/one.svg}}\n"
            f"\\caption{{Figure number {index} with $R_{{\\mathrm{{T}}}}$.}}\\label{{fig:f{index}}}\n\\end{{figure}}"
        )
    body.append(
        "\\begin{table}[t]\n\\caption{Fictional parameters.}\\label{tab:one}\\centering\n"
        "\\begin{tabular}{lcr}\n\\toprule\nName & Unit & Value \\\\\n\\midrule\nAlpha & mV & 1.5 \\\\\nBeta & pF & 20 \\\\\n\\bottomrule\n\\end{tabular}\n\\end{table}"
    )
    body.append(equations or "\\begin{equation}\nE = mc^2\\label{eq:one}\n\\end{equation}\nSee Eq.~\\eqref{eq:one}.")
    body.append("\\section*{Acknowledgment}\nThanks to nobody in particular.")
    body.append(BIB_TAIL)
    return "\n".join(body)


def write_project(root: Path, main_text: str) -> Path:
    project = root / "project"
    project.mkdir(parents=True)
    (project / "main.tex").write_text(main_text, encoding="utf-8")
    shutil.copy(GENERIC_ARTICLE / "refs.bib", project / "refs.bib")
    shutil.copytree(GENERIC_ARTICLE / "figures", project / "figures")
    return project


def write_profile(directory: Path, name: str, data: dict[str, Any], extends: str = "generic") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(
        yaml.safe_dump({"profile_schema_version": 1, "id": name, "extends": extends, **data}, sort_keys=False), encoding="utf-8"
    )


def build_project(root: Path, main_text: str, profile: str, *, profile_dirs: list[Path] | None = None, extra: dict[str, Any] | None = None) -> BuildResult:
    project = write_project(root, main_text)
    data: dict[str, Any] = {
        "schema_version": 1,
        "project": {"name": "layout-fixture", "root": str(project), "main": "main.tex"},
        "profile": profile,
        "workspace": {"root": str(root / "workspace")},
        "output": {"directory": str(root / "out")},
        "tools": {"timeout_s": 90},
        **(extra or {}),
    }
    if profile_dirs:
        data["profile_dirs"] = [str(d) for d in profile_dirs]
    config_path = root / "config.yaml"
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return run_build(load_config(config_path))


@dataclass
class FinalDoc:
    """Read-only view of a final DOCX."""

    path: Path
    document: etree._Element
    body: etree._Element

    @classmethod
    def open(cls, path: Path) -> FinalDoc:
        with zipfile.ZipFile(path) as archive:
            document = etree.fromstring(archive.read("word/document.xml"))
        return cls(path, document, document.find(w("body")))

    def paragraphs(self, style: str) -> list[etree._Element]:
        return [p for p in self.document.iter(w("p")) if (s := p.find(f"{w('pPr')}/{w('pStyle')}")) is not None and s.get(w("val")) == style]

    def texts(self, style: str) -> list[str]:
        return [" ".join(text_of(p).split()) for p in self.paragraphs(style)]

    def tables(self, style: str | None = None) -> list[etree._Element]:
        found = []
        for table in self.document.iter(w("tbl")):
            node = table.find(f"{w('tblPr')}/{w('tblStyle')}")
            if style is None or (node is not None and node.get(w("val")) == style):
                found.append(table)
        return found

    def sections(self) -> list[dict[str, Any]]:
        out = []
        for sect in self.body.iter(w("sectPr")):
            cols, size, margin, kind = sect.find(w("cols")), sect.find(w("pgSz")), sect.find(w("pgMar")), sect.find(w("type"))
            out.append({
                "type": kind.get(w("val")) if kind is not None else "nextPage",
                "cols": int(cols.get(w("num"))), "space": int(cols.get(w("space"))),
                "w": int(size.get(w("w"))), "h": int(size.get(w("h"))),
                "margins": {k: int(margin.get(w(k))) for k in ("top", "bottom", "left", "right")},
            })
        return out

    def extents_in(self) -> list[float]:
        return [int(e.get("cx")) / 914400 for e in self.document.iter(qn("wp:inline") + "/" + qn("wp:extent"))] or [
            int(e.get("cx")) / 914400 for e in self.document.iter(f"{{{NS['wp']}}}extent")
        ]

    def visible_text(self) -> str:
        return "\n".join(text_of(p) for p in self.document.iter(w("p")))


def layout_report(result: BuildResult) -> dict[str, Any]:
    assert result.layout_report is not None
    return json.loads(result.layout_report.read_text(encoding="utf-8"))


def problems(result: BuildResult) -> list[str]:
    return [str(f) for f in result.findings if f.severity.value != "info"]
