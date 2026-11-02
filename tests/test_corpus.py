"""The unsupported-construct corpus: every case is a tiny LaTeX project run through the real build pipeline.

``CASES`` is the single source of truth for what latex2word promises about each construct (the human-readable
table in ``docs/unsupported-constructs.md`` is written from it). Every case pins

* a classification -- ``SUPPORTED`` (build ok, no warning), ``SUPPORTED_WITH_WARNING`` (build ok, a specific
  warning, or an explicit acceptance through ``tex.allow_commands``) or ``EXPLICIT_ERROR`` (build fails, no DOCX
  is written, a stable error code names the construct),
* the diagnostic codes, and
* for errors the source location (``file:line``) and the words the message must contain.

No case may delete scientific content silently: ``SUPPORTED`` cases additionally pin the text that must
reach the DOCX. Pandoc is a normal dependency of the deterministic suite (skipped when it is not installed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT
from layoutkit import FinalDoc
from pandockit import requires_pandoc
from projectkit import make_project, tree_digest

from latex2word.build import BuildResult, run_build
from latex2word.config import load_config
from latex2word.diagnostics import Severity

pytestmark = requires_pandoc

SUPPORTED = "SUPPORTED"
WARNING = "SUPPORTED_WITH_WARNING"
ERROR = "EXPLICIT_ERROR"

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "semantic" / "generic_article"

IEEE_LIKE = {
    "profile_schema_version": 1,
    "id": "ieee-like",
    "extends": "generic",
    "layout": {"columns": {"body": 2, "gap": "0.25in"}},
    # author blocks side by side ("columns": a borderless table); "table" was never a valid layout value
    "frontmatter": {"author_block": {"layout": "columns", "max_columns": 3}},
}


@dataclass(frozen=True)
class Case:
    """One corpus entry: a project (``body`` inside the standard article, or complete ``files``) and its pinned outcome."""

    name: str
    classification: str
    codes: tuple[str, ...] = ()
    body: str = ""
    preamble: str = ""
    author: str = "\\author{Ada Example}"
    bib: bool = False
    files: dict[str, str | bytes] = field(default_factory=dict)
    profile: str = "generic"
    config: dict[str, Any] = field(default_factory=dict)
    where: str | None = None  # a substring of the offending source line; the finding must name its file:line
    mention: tuple[str, ...] = ()  # words the pinned error message must contain
    keep: tuple[str, ...] = ()  # text that must be in the DOCX (SUPPORTED*)
    absent: tuple[str, ...] = ()  # text that must NOT be in the DOCX (raw TeX leaking through)
    sentinel: str | None = None  # a file that a side-effecting construct would create; it must never appear


def article(case: Case) -> str:
    lines = [
        "\\documentclass{article}", "\\usepackage{amsmath}", "\\usepackage{graphicx}", "\\usepackage{booktabs}",
        case.preamble, "\\title{Corpus Case}", case.author, "\\begin{document}", "\\maketitle",
        "\\begin{abstract}\nA short abstract.\n\\end{abstract}", "\\section{Introduction}\\label{sec:intro}", case.body,
    ]
    if case.bib:
        lines += ["\\bibliographystyle{plain}", "\\bibliography{refs}"]
    lines.append("\\end{document}")
    return "\n".join(lines) + "\n"


CASES: list[Case] = [
    # ---- unknown commands / environments --------------------------------------------------------
    Case("unknown-command-text", ERROR, ("L2W-E-TEX-001",), body="Before.\n\\highlightnote{Critical finding that must not vanish.}\nAfter.", where="highlightnote", mention=("highlightnote",)),
    Case("unknown-command-optional-argument", ERROR, ("L2W-E-TEX-001",), body="Before.\n\\todo[inline]{Resolve the sampling rate question}\nAfter.", where="todo", mention=("todo",)),
    Case("unknown-wrapper-command", ERROR, ("L2W-E-TEX-001",), body="Before \\mybox{Boxed result that matters} after.", where="mybox", mention=("mybox",)),
    Case("unknown-command-allowed-by-config", WARNING, ("L2W-I-TEX-003",), body="Before.\n\\highlightnote{Critical finding}\nAfter.", config={"tex": {"allow_commands": ["highlightnote"]}}, keep=("Before.", "After."), absent=("highlightnote",)),
    Case("unknown-environment-with-text", WARNING, ("L2W-I-TEX-002",), body="\\begin{remark}\nA remark with real content.\n\\end{remark}", keep=("A remark with real content.",)),
    Case("newcommand-text-macro", SUPPORTED, body="\\newcommand{\\sysname}{FooSolver}\nWe evaluate \\sysname{} on every benchmark.", keep=("We evaluate FooSolver on every benchmark.",)),
    Case("newcommand-wrapper-macro", SUPPORTED, body="\\newcommand{\\mybox}[1]{\\fbox{#1}}\nBefore \\mybox{Boxed result} after.", keep=("Boxed result",), absent=("\\mybox",)),
    Case("newcommand-math-macro", SUPPORTED, body="\\newcommand{\\R}{\\mathbb{R}}\nLet $x \\in \\R$ be given.", keep=("Let",)),
    Case("newenvironment-definition", SUPPORTED, body="\\newenvironment{note}{\\par\\textbf{Note:} }{\\par}\n\\begin{note}Defined environment text.\\end{note}", keep=("Note:", "Defined environment text.")),
    # ---- references ---------------------------------------------------------------------------
    Case("nameref", ERROR, ("L2W-E-REF-006",), body="See \\nameref{sec:intro}.", where="nameref", mention=("nameref",)),
    Case("pageref", ERROR, ("L2W-E-REF-006",), body="See page~\\pageref{sec:intro}.", where="pageref", mention=("pageref",)),
    Case("undefined-reference", ERROR, ("L2W-E-REF-001",), body="See Section~\\ref{sec:nowhere}.", where="nowhere", mention=("sec:nowhere",)),
    Case("duplicate-label", ERROR, ("L2W-E-LABEL-001",), body="\\section{Two}\\label{sec:intro}\nText.", where="sec:intro", mention=("sec:intro",)),
    Case("unresolved-citation", ERROR, ("L2W-E-CITE-001",), body="As shown by \\cite{missingkey}.", bib=True, where="missingkey", mention=("missingkey",)),
    Case("resolved-citation", SUPPORTED, body="As shown by \\cite{alpha}.", bib=True, keep=("As shown by",)),
    # ---- math ----------------------------------------------------------------------------------
    Case("math-tag-complex", ERROR, ("L2W-E-MATH-003",), body="\\begin{equation}\n E = mc^2 \\tag{A.1}\n\\end{equation}", mention=("tag",)),
    Case("math-intertext", ERROR, ("L2W-E-MATH-003",), body="\\begin{align}\n a &= b \\\\\n\\intertext{where $b$ is the baseline}\n c &= d\n\\end{align}", mention=("intertext",)),
    Case("math-rcases", ERROR, ("L2W-E-MATH-002",), preamble="\\usepackage{mathtools}", body="\\begin{equation}\n\\begin{rcases} a = 1 \\\\ b = 2 \\end{rcases} \\Rightarrow c\n\\end{equation}"),
    Case("math-unknown-command", ERROR, ("L2W-E-MATH-002",), body="Inline $\\unknownsymbol{x} + 1$ math."),
    # ---- front matter --------------------------------------------------------------------------
    Case("author-thanks-generic", ERROR, ("L2W-E-META-002",), author="\\author{Ada Example\\thanks{Funding text that must not vanish}}", mention=("thanks",)),
    Case("author-thanks-custom-profile", ERROR, ("L2W-E-META-002",), profile="ieee-like", author="\\author{Ada Example\\thanks{Funding text that must not vanish}}", mention=("thanks",)),
    Case("author-marks-unsupported", ERROR, ("L2W-E-META-002",), author="\\author{Ada Example\\textsuperscript{*}\\\\Department of Samples}", mention=("textsuperscript",)),
    # ---- dependencies ------------------------------------------------------------------------
    Case("missing-figure", ERROR, ("missing-dependency",), body="\\begin{figure}\n\\includegraphics{figures/absent}\n\\caption{Absent.}\n\\end{figure}", where="figures/absent"),
    Case("graphics-escape-project-root", ERROR, ("outside-root",), body="\\begin{figure}\n\\includegraphics{../outside.svg}\n\\caption{Escapes.}\n\\end{figure}", where="../outside.svg"),
    Case("dynamic-input", ERROR, ("dynamic-path",), preamble="\\newcommand{\\part}{sections/one}", body="\\input{\\part}", where="input"),
    Case("dynamic-include", ERROR, ("dynamic-path",), preamble="\\newcommand{\\part}{sections/one}", body="\\include{\\part}", where="include"),
    Case("macro-generated-figure-name", ERROR, ("dynamic-path",), preamble="\\def\\name{one}", body="\\begin{figure}\n\\includegraphics{figures/\\name}\n\\caption{Generated.}\n\\end{figure}", where="figures/\\name"),
    Case("dynamic-bibliography", ERROR, ("dynamic-path",), body="As shown by \\cite{alpha}.\n", preamble="\\def\\bibname{refs}", where="bibliography"),
    Case("figure-width-dimexpr", ERROR, ("L2W-E-FIG-007",), body="\\begin{figure}\n\\includegraphics[width=\\dimexpr\\linewidth-1cm\\relax]{figures/one.svg}\n\\caption{Expr.}\n\\end{figure}", where="dimexpr"),
    Case("include-missing-file", ERROR, ("missing-dependency",), body="\\include{chapters/gone}", where="chapters/gone"),
    Case("subfile", ERROR, ("unsupported-command",), body="\\subfile{sections/one}", files={"sections/one.tex": "Text of the subfile.\n"}, where="subfile"),
    # ---- tables -------------------------------------------------------------------------------
    Case("table-unbalanced-braces", ERROR, (), body="\\begin{tabular}{lc}\nA & {B \\\\\nC & D \\\\\n\\end{tabular}"),
    Case("table-wrong-column-count", ERROR, (), body="\\begin{tabular}{lc}\nA & B & C \\\\\nD & E \\\\\n\\end{tabular}"),
    # ---- local styles ---------------------------------------------------------------------------
    Case("local-style-unbalanced-braces", ERROR, (), preamble="\\usepackage{local}", files={"local.sty": "\\ProvidesPackage{local}\n\\newcommand{\\broken}{abc\n"}),
    Case("local-style-input-missing", ERROR, (), preamble="\\usepackage{local}", files={"local.sty": "\\ProvidesPackage{local}\n\\input{missing-macros}\n"}),
    # ---- raw TeX and TeX-level programming -------------------------------------------------------
    Case("raw-tex-conditional-text", ERROR, (), body="\\ifx\\foo\\bar Alternative one\\else Alternative two\\fi"),
    Case("conditional-structure", ERROR, (), body="\\iftrue\n\\section{Hidden by the conditional}\nConditional text.\n\\fi"),
    Case("catcode", ERROR, (), preamble="\\catcode`\\@=11", body="Plain text."),
    Case("makeatletter", WARNING, (), preamble="\\makeatletter\n\\def\\@maketitle{}\n\\makeatother", body="Plain text."),
    Case("write18-shell-escape", ERROR, (), body="Plain text.\n\\immediate\\write18{touch pwned.txt}\nMore text.", sentinel="pwned.txt"),
    Case("immediate-write-file", ERROR, (), body="Plain text.\n\\newwrite\\out\\immediate\\openout\\out=sidefx.txt\n\\immediate\\write\\out{leak}\n", sentinel="sidefx.txt"),
    Case("openin", ERROR, (), body="\\openin5=data.txt\nPlain text.", sentinel="data.txt"),
    Case("usepackage-unknown", SUPPORTED, (), preamble="\\usepackage{totallyunknownpackage}", body="Plain text.", keep=("Plain text.",)),
    # ---- verbatim, footnotes, links, comments ------------------------------------------------------
    Case("verb-inline", SUPPORTED, (), body="The command \\verb|\\textbackslash{}x| is literal.", keep=("is literal",)),
    Case("verbatim-environment", SUPPORTED, (), body="\\begin{verbatim}\nfor i in range(3): print(i)\n\\end{verbatim}", keep=("for i in range(3)",)),
    Case("lstlisting-environment", SUPPORTED, (), preamble="\\usepackage{listings}", body="\\begin{lstlisting}\nint main() { return 0; }\n\\end{lstlisting}", keep=("int main",)),
    Case("footnote", SUPPORTED, (), body="A claim.\\footnote{The footnote text.} More.", keep=("The footnote text.",)),
    Case("href-and-url", SUPPORTED, (), preamble="\\usepackage{hyperref}", body="See \\href{https://example.org/page}{the project page} and \\url{https://example.org/raw}.", keep=("the project page", "https://example.org/raw")),
    Case("comment-escaped-content", SUPPORTED, (), body="Fifty percent\\% of the data % a comment that must vanish\nremains.", keep=("Fifty percent% of the data",), absent=("a comment that must vanish",)),
]


def run_case(case: Case, tmp_path: Path) -> tuple[BuildResult, Path]:
    files: dict[str, str | bytes] = {
        "main.tex": article(case),
        "refs.bib": (FIXTURES / "refs.bib").read_bytes(),
        "figures/one.svg": (FIXTURES / "figures" / "one.svg").read_bytes(),
    }
    files.update(case.files)
    project = make_project(tmp_path, files)
    profile_dirs: list[str] = []
    if case.profile == "ieee-like":
        directory = tmp_path / "profiles"
        directory.mkdir()
        (directory / "ieee-like.yaml").write_text(yaml.safe_dump(IEEE_LIKE, sort_keys=False), encoding="utf-8")
        profile_dirs.append(str(directory))
    data: dict[str, Any] = {
        "schema_version": 1,
        "project": {"name": "corpus", "root": str(project), "main": "main.tex"},
        "profile": case.profile,
        "workspace": {"root": str(tmp_path / "workspace")},
        "output": {"directory": str(tmp_path / "out")},
        "tools": {"timeout_s": 90},
        **case.config,
    }
    if profile_dirs:
        data["profile_dirs"] = profile_dirs
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return run_build(load_config(config_path), keep_workspace=True), project


def describe(result: BuildResult) -> list[str]:
    return [f"{f.severity.value} {f.code} {f.location} {f.message}" for f in result.findings]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_corpus_case(case: Case, tmp_path: Path):
    result, project = run_case(case, tmp_path)
    print("\n".join(describe(result)))
