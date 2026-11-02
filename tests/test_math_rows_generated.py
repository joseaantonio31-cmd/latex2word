"""R9: generated matrix for the math row splitter (F-01 showed that hand-picked cases were not enough).

Invariant: meaningful row content never disappears silently. Every case is generated from parts whose expected content
is known independently of the filter:

* environments: align, align*, aligned, split, gather*, alignat*, flalign*, array{rl} and eqnarray* (kernel);
* row breaks: \\\\ and \\\\* with and without a spacing argument [2pt] (attached, as LaTeX reads it as spacing);
* row starts: a plain cell, a bracketed cell [x,y] (directly after a spacing argument, after a space or a newline),
  a \\left( ... \\right) cell ("\\\\[x,y]" right after the break is LaTeX's spacing syntax and is not generated);
* cells with alignment & and escaped \\&; first and later rows.

Kernel environments (array, eqnarray*) read "\\\\ [..]" across spaces as spacing, so no bracketed content follows a
space there (that would not be valid LaTeX).

Pipeline under test: ``l2w_math.analyze`` on the Pandoc-3.12-shaped math string -> texmath (plain Pandoc, DOCX writer)
-> OMML. Oracle: an independent linearisation of the OMML (text runs plus delimiter characters), compared with the
tokens of the generated source. All cases go through one Lua run and one Pandoc run.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from lxml import etree
from pandockit import PANDOC, requires_pandoc

pytestmark = requires_pandoc

FILTER_DIR = Path(__file__).resolve().parents[1] / "src" / "latex2word" / "filters"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

# name -> (begin, end, kernel, cells per row as (left, right) or single)
ENVIRONMENTS = {
    "align*": ("\\begin{align*}", "\\end{align*}", False, 2),
    "align": ("\\begin{align}", "\\end{align}", False, 2),  # numbered: all but the last row get \nonumber
    "aligned": ("\\begin{equation*}\\begin{aligned}", "\\end{aligned}\\end{equation*}", False, 2),
    "split": ("\\begin{equation*}\\begin{split}", "\\end{split}\\end{equation*}", False, 2),
    "gather*": ("\\begin{gather*}", "\\end{gather*}", False, 1),
    "alignat*": ("\\begin{alignat*}{1}", "\\end{alignat*}", False, 2),
    "flalign*": ("\\begin{flalign*}", "\\end{flalign*}", False, 2),
    "array-rl": ("\\begin{equation*}\\begin{array}{rl}", "\\end{array}\\end{equation*}", True, 2),
    "eqnarray*": ("\\begin{eqnarray*}", "\\end{eqnarray*}", True, 3),
}
BREAKS = ["\\\\", "\\\\*", "\\\\[2pt]", "\\\\*[2pt]"]
STARTS = ["plain", "bracket", "space-bracket", "newline-bracket", "left-right"]


def _row(env: str, cells: int, index: int, start: str, escaped: bool) -> tuple[str, list[str]]:
    """Source of row ``index`` and the tokens that must appear in the OMML."""
    k = index + 1
    tokens: list[str] = []
    if start == "plain":
        lead, lead_tok = f"u_{{{k}}}", f"u{k}"
    elif start == "left-right":
        lead, lead_tok = f"\\left( v_{{{k}}} \\right)", f"(v{k})"
    else:
        lead, lead_tok = f"[x_{{{k}}},y_{{{k}}}]", f"[x{k},y{k}]"
    prefix = {"space-bracket": " ", "newline-bracket": "\n"}.get(start, "")
    tokens.append(lead_tok)
    amp = " \\& w" if escaped else ""
    if escaped:
        tokens.append("&w")
    if cells == 1:
        return prefix + lead + amp, tokens
    if cells == 2:
        tokens.append(f"=z{k}")
        return prefix + lead + f" &= z_{{{k}}}" + amp, tokens
    tokens += ["=", f"z{k}"]
    return prefix + lead + f" &=& z_{{{k}}}" + amp, tokens


def generate() -> list[dict]:
    cases = []
    for env, (begin, end, kernel, cells) in ENVIRONMENTS.items():
        for brk, start, escaped, first_bracket in itertools.product(BREAKS, STARTS, (False, True), (False, True)):
            if kernel and start in ("space-bracket", "newline-bracket"):
                continue  # not valid LaTeX: the kernel \\ reads "[..]" across spaces as a dimension
            if start == "bracket" and not brk.endswith("]"):
                continue  # "\\[x,y]" is LaTeX's own spacing-argument syntax (a dimension error), not content
            rows, tokens = [], []
            first_start = "bracket" if first_bracket else "plain"
            for index, row_start in enumerate((first_start, start)):
                text, toks = _row(env, cells, index, row_start, escaped and index == 1)
                if env == "align" and index == 0:
                    text += " \\nonumber"
                rows.append(text)
                tokens += toks
            source = begin + " " + rows[0] + " " + brk + rows[1] + " " + end
            cases.append({"id": f"{env}|{brk}|{start}|esc={escaped}|first={first_start}", "tex": source,
                          "tokens": tokens, "escaped": 1 if escaped else 0})
    return cases


def _linear(element) -> str:
    tag = etree.QName(element).localname
    if tag == "t":
        return element.text or ""
    if tag == "d":
        pr = element.find(f"{{{M}}}dPr")
        b = pr.find(f"{{{M}}}begChr") if pr is not None else None
        e = pr.find(f"{{{M}}}endChr") if pr is not None else None
        inner = "".join(_linear(c) for c in element if etree.QName(c).localname == "e")
        return (b.get(f"{{{M}}}val") if b is not None else "(") + inner + (e.get(f"{{{M}}}val") if e is not None else ")")
    return "".join(_linear(c) for c in element if isinstance(c.tag, str))


_LUA = r"""
package.path = '%s/?.lua;' .. package.path
local M = require('l2w_math')
local cases = pandoc.json.decode(io.open('%s'):read('a'), false)
local out = {}
for i, c in ipairs(cases) do
  local r = M.analyze(c.tex, true)
  local codes = {}
  for _, e in ipairs(r.errors) do codes[#codes + 1] = e.code end
  out[i] = { tex = r.tex, codes = table.concat(codes, ',') }
end
print(pandoc.json.encode(out))
"""


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rows")
    cases = generate()
    (tmp / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (tmp / "run.lua").write_text(_LUA % (FILTER_DIR.as_posix(), (tmp / "cases.json").as_posix()), encoding="utf-8")
    run = subprocess.run([PANDOC, "lua", str(tmp / "run.lua")], capture_output=True, text=True, encoding="utf-8")
    if run.returncode != 0 and "unknown" in run.stderr.lower():
        pytest.skip("this Pandoc has no `pandoc lua`")
    assert run.returncode == 0, run.stderr
    analyzed = json.loads(run.stdout)
    body = "\n\n".join(f"$${a['tex']}$$" for a in analyzed)
    (tmp / "all.tex").write_text(f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n", encoding="utf-8")
    subprocess.run([PANDOC, "-f", "latex", "-t", "docx", "-o", "all.docx", "all.tex"], cwd=tmp, check=True, capture_output=True)
    root = etree.fromstring(zipfile.ZipFile(tmp / "all.docx").read("word/document.xml"))
    paras = list(root.iter(f"{{{M}}}oMathPara"))
    assert len(paras) == len(cases)
    lines = ["".join(_linear(p)).replace("\u200b", "").replace(" ", "") for p in paras]
    return list(zip(cases, analyzed, lines))


def test_matrix_size(results):
    assert len(results) >= 250  # recorded in reports/audit/remediation-phase2.md


def test_no_case_fails_or_errors(results):
    bad = [(c["id"], a["codes"]) for c, a, _ in results if a["codes"]]
    assert bad == []


def test_meaningful_row_content_never_disappears(results):
    missing = [(c["id"], [t for t in c["tokens"] if t not in line], line) for c, _, line in results
               if any(t not in line for t in c["tokens"])]
    assert missing == [], missing[:5]


def test_spacing_arguments_never_become_content(results):
    leaked = [(c["id"], line) for c, _, line in results if "2pt" in line]
    assert leaked == []


def test_escaped_ampersands_are_exactly_the_literal_ones(results):
    wrong = [(c["id"], line) for c, _, line in results if line.count("&") != c["escaped"]]
    assert wrong == []


def test_f12_multiple_numbered_rows_still_fail_loudly(tmp_path):
    """F-12 is a documented limitation, not implemented in phase 2: it must stay an explicit L2W-E-MATH-003."""
    cases = [{"tex": "\\begin{align} a &= b \\\\ c &= d \\end{align}"}, {"tex": "\\begin{gather} a \\\\ b \\end{gather}"},
             {"tex": "\\begin{align} a &= b \\nonumber \\\\ c &= d \\end{align}"}]
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (tmp_path / "run.lua").write_text(_LUA % (FILTER_DIR.as_posix(), (tmp_path / "cases.json").as_posix()), encoding="utf-8")
    out = json.loads(subprocess.run([PANDOC, "lua", str(tmp_path / "run.lua")], capture_output=True, text=True, check=True).stdout)
    assert [o["codes"] for o in out] == ["L2W-E-MATH-003", "L2W-E-MATH-003", ""]
