"""Constructs Pandoc would silently drop must surface as diagnostics."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from pandockit import requires_pandoc
from projectkit import make_project

from latex2word.diagnostics import Severity
from latex2word.pandoc_adapter import PandocTool
from latex2word.source_graph import build_source_graph
from latex2word.texprobe import collect_usage, defined_names, mask_math, probe

pytestmark = requires_pandoc


def run_probe(tmp_path: Path, files: dict[str, str], **kwargs):
    root = make_project(tmp_path, files)
    graph = build_source_graph(root, root / "main.tex")
    tool = PandocTool(shutil.which("pandoc"), "3.12", "pandoc 3.12")
    return probe(graph, tool, tmp_path / "probe", **kwargs)


def doc(body: str, preamble: str = "") -> dict[str, str]:
    return {"main.tex": f"\\documentclass{{article}}\n{preamble}\\begin{{document}}\n{body}\n\\end{{document}}\n"}


def by_code(result) -> dict[str, list]:
    out: dict[str, list] = {}
    for finding in result.findings:
        out.setdefault(finding.code, []).append(finding)
    return out


def test_unknown_command_with_prose_arguments_is_an_error_with_its_location(tmp_path: Path):
    result = run_probe(tmp_path, doc("Hello.\n\\authorblock{Some Name, Some Place} text"))
    (finding,) = by_code(result)["L2W-E-TEX-001"]
    assert finding.severity is Severity.ERROR and "\\authorblock" in finding.message
    assert finding.location.file == "main.tex" and finding.location.line == 4


def test_unknown_switch_without_text_is_only_informational(tmp_path: Path):
    result = run_probe(tmp_path, doc("\\fancyswitch Hello \\spacer{3pt} world."))
    assert set(by_code(result)) == {"L2W-I-TEX-001"}
    assert {f.severity for f in result.findings} == {Severity.INFO}


def test_commands_whose_loss_changes_meaning_are_errors(tmp_path: Path):
    result = run_probe(tmp_path, doc("See \\nameref{x} and \\pageref{x}, \\vref{x} (known) and \\citeauthoryear{k} and \\labelcref{x}."))
    codes = {re.search(r"\\[A-Za-z]+", f.message).group(0): f.code for f in result.findings}
    assert codes == {
        "\\nameref": "L2W-E-REF-006", "\\pageref": "L2W-E-REF-006", "\\labelcref": "L2W-E-REF-006",
        "\\citeauthoryear": "L2W-E-CITE-002",
    }
    assert all(f.severity is Severity.ERROR for f in result.findings)


def test_commands_pandoc_understands_are_not_reported(tmp_path: Path):
    body = (
        "\\textbf{a} \\emph{b} \\textit{c} \\footnote{d} \\url{http://x.y} \\href{http://x.y}{t}\n\n\\cite{k} \\citep{k} \\autocite{k}\n\n"
        "\\ref{a} \\eqref{a} \\label{a}\n\n\\begin{itemize}\\item x\\end{itemize}\n\n"
        "\\scriptsize \\centering \\vspace{2pt} \\setlength{\\tabcolsep}{3pt} \\bibliographystyle{plain}"
    )
    assert run_probe(tmp_path, doc(body)).findings == []


def test_profile_accepted_commands_and_environments_are_downgraded(tmp_path: Path):
    files = doc("\\blockauthor{Name Here}\n\\begin{keywordsenv}a, b\\end{keywordsenv}")
    plain = run_probe(tmp_path / "a", files)
    assert "L2W-E-TEX-001" in by_code(plain) and "L2W-I-TEX-002" in by_code(plain)
    accepted = run_probe(tmp_path / "b", files, accepted_commands={"blockauthor"}, accepted_environments={"keywordsenv"})
    assert set(by_code(accepted)) == {"L2W-I-TEX-003"}


def test_adapter_consumed_names_are_informational_only(tmp_path: Path):
    files = doc("\\blockauthor{Name Here}\n\\begin{keywordsenv}a, b\\end{keywordsenv}")
    result = run_probe(tmp_path, files, consumed_commands={"blockauthor"}, consumed_environments={"keywordsenv"})
    assert set(by_code(result)) == {"L2W-I-TEX-004"}
    assert {f.severity for f in result.findings} == {Severity.INFO}


def test_user_allow_list_downgrades_to_info_naming_the_project_configuration(tmp_path: Path):
    files = doc("\\blockauthor{Name Here}\n\\begin{keywordsenv}a, b\\end{keywordsenv}")
    result = run_probe(tmp_path, files, allowed_commands={"blockauthor"}, allowed_environments={"keywordsenv"})
    assert set(by_code(result)) == {"L2W-I-TEX-003"}
    assert all("project configuration" in f.message for f in result.findings)


def test_macros_defined_by_the_document_or_a_local_style_are_trusted(tmp_path: Path):
    files = {
        **doc("\\mine{x} \\fromstyle{y} \\begin{boxed}z\\end{boxed}", "\\usepackage{localstyle}\n\\newcommand{\\mine}[1]{\\emph{#1}}\n"),
        "localstyle.sty": "\\newcommand{\\fromstyle}[1]{\\textbf{#1}}\\newenvironment{boxed}{\\begin{quote}}{\\end{quote}}",
    }
    assert run_probe(tmp_path, files).findings == []


def test_macros_defined_only_in_a_document_class_are_not_trusted(tmp_path: Path):
    files = {
        "main.tex": "\\documentclass{localclass}\n\\begin{document}\n\\classonly{text that is lost}\n\\end{document}\n",
        "localclass.cls": "\\LoadClass{article}\n\\newcommand{\\classonly}[1]{\\emph{#1}}\n",
    }
    result = run_probe(tmp_path, files)
    assert [f.code for f in result.findings] == ["L2W-E-TEX-001"] and "classonly" in result.findings[0].message


def test_math_comments_verbatim_and_the_preamble_are_not_scanned(tmp_path: Path):
    files = doc(
        "$\\unknownmath{x} + \\alpha$ and \\[\\anothermath{y}\\]\n% \\commented{lost}\n\\begin{verbatim}\\literal{x}\\end{verbatim}",
        "\\newcommand{\\helper}{\\unknownpre{preamble}}\\usepackage{tikz}\\title{\\titlemacro{T}}\n",
    )
    assert run_probe(tmp_path, files).findings == []


def test_files_included_before_the_document_begins_are_preamble(tmp_path: Path):
    files = {
        "main.tex": "\\documentclass{article}\n\\input{macros}\n\\begin{document}\nBody \\inbody{x}.\n\\end{document}\n",
        "macros.tex": "\\usepackage{amsmath}\\somethingnew{in preamble file}\n",
    }
    result = run_probe(tmp_path, files)
    assert [f.message.split()[0] for f in result.findings] == ["\\inbody"]


def test_includegraphics_options_are_not_commands_to_probe(tmp_path: Path):
    assert run_probe(tmp_path, doc("\\includegraphics[width=0.5\\linewidth]{x.png}")).findings == []


def test_mask_math_blanks_every_math_form_and_keeps_offsets():
    text = "a $x$ b $$y$$ c \\(z\\) d \\[w\\] e \\begin{align}v\\end{align} f \\$ g"
    masked = mask_math(text)
    assert len(masked) == len(text)
    assert masked.split() == ["a", "b", "c", "d", "e", "f", "\\$", "g"]


def test_usage_collection_and_defined_names_helpers(tmp_path: Path):
    root = make_project(tmp_path, doc("\\a{1} \\a{2} \\begin{e}x\\end{e}", "\\newcommand{\\b}{}\\def\\c{}\\newenvironment{e2}{}{}\n"))
    graph = build_source_graph(root, root / "main.tex")
    assert {"b", "c", "e2"} <= defined_names(graph)
    usage = collect_usage(graph, set())
    assert [(k, u.count) for k, u in usage.items()] == [(("command", "a"), 2), (("environment", "e"), 1)]


def test_probe_runs_in_the_given_directory_and_reports_its_command(tmp_path: Path):
    result = run_probe(tmp_path, doc("\\something{x y z}"))
    assert (tmp_path / "probe" / "probe.tex").is_file() and result.pandoc_run["exit_code"] == 0
    assert "latex+raw_tex" in result.pandoc_run["argv"]
    assert result.unknown == ["command:something"]
