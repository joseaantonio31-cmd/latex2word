"""Alignment markers must not leak into Word equations; legitimate content must survive.

Evidence (Pandoc 3.12, texmath): ``aligned``/``split``/``align`` rows -- and any
``array`` whose columns alternate right/left -- become ``<m:eqArr>`` whose
alignment markers are literal ``&`` text runs, indistinguishable from an escaped
``\\&``. The filter therefore rewrites the TeX before the writer.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from pandockit import codes, document, requires_pandoc, run_filter, walk

pytestmark = requires_pandoc

M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"


def omml(tmp_path: Path, body: str, **kwargs):
    """``(texts of every m:t, element tree root, report)`` of the DOCX produced for ``body``."""
    path, report = run_filter(tmp_path, document(body), to="docx", output="out.docx", **kwargs)
    root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    texts = ["".join(t.itertext()) for t in root.iter(f"{M}t")]
    return texts, root, report


def display(body: str) -> str:
    return f"\n\\[\n{body}\n\\]\n"


ALIGNED_FORMS = {
    "aligned-one-row": "\\begin{aligned} a &= b \\end{aligned}",
    "aligned-rows": "\\begin{aligned} a &= b \\\\ c &= d \\\\ &= e \\end{aligned}",
    "aligned-two-pairs": "\\begin{aligned} a &= b & c &= d \\\\ e &= f & g &= h \\end{aligned}",
    "split": "\\begin{split} a &= b \\\\ &= c \\end{split}",
    "alignedat": "\\begin{alignedat}{2} a &= b & c &= d \\\\ e &= f & g &= h \\end{alignedat}",
    "align-star": "\\begin{align*} a &= b \\\\ c &= d \\end{align*}",
    "eqnarray-star": "\\begin{eqnarray*} a &=& b \\\\ c &=& d \\end{eqnarray*}",
    "array-rl": "\\begin{array}{rl} a &= b \\\\ c &= d \\end{array}",
    "array-rl-with-rules": "\\begin{array}{|r|l|} a &= b \\\\ c &= d \\end{array}",
    "array-rlr": "\\begin{array}{rlr} a &= b & c \\\\ d &= e & f \\end{array}",
}


@pytest.mark.parametrize("name", sorted(ALIGNED_FORMS))
def test_alignment_environments_leave_no_ampersand_and_no_equation_array(tmp_path: Path, name: str):
    texts, root, report = omml(tmp_path, display(ALIGNED_FORMS[name]))
    assert "&" not in "".join(texts)
    assert list(root.iter(f"{M}eqArr")) == []
    assert report["diagnostics"] == []


def test_rows_cells_and_operators_survive_normalisation(tmp_path: Path):
    texts, root, _ = omml(tmp_path, display("\\begin{aligned} a &= b + c \\\\ d &\\le e \\end{aligned}"))
    rows = list(root.iter(f"{M}mr"))
    assert len(rows) == 2
    row_text = ["".join(t.text or "" for t in row.iter(f"{M}t")) for row in rows]
    assert row_text[0].replace(" ", "") == "a=b+c" and row_text[1].replace(" ", "") == "d≤e"


def test_escaped_ampersand_is_preserved_while_structural_ones_are_removed(tmp_path: Path):
    texts, _, report = omml(tmp_path, display("\\begin{aligned} a &= b \\& c \\\\ d &= e \\end{aligned}"))
    assert "".join(texts).count("&") == 1
    assert report["math"]["literal_ampersands"] == 1 and report["math"]["normalized_alignment"] == 1


def test_escaped_ampersand_outside_alignment_environments_is_untouched(tmp_path: Path):
    texts, _, report = omml(tmp_path, "Inline $a \\& b$ and\n\\[\nx \\& y\n\\]\n")
    assert "".join(texts).count("&") == 2 and report["math"]["literal_ampersands"] == 2
    assert report["math"]["normalized_alignment"] == 0


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("\\begin{array}{cc} a & b \\\\ c & d \\end{array}", "abcd"),
        ("f(x)=\\begin{cases} 1 & x>0 \\\\ 0 & x\\le 0 \\end{cases}", "f(x)=1x>00x≤0"),
        ("\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}", "abcd"),
        ("\\begin{vmatrix} a & b \\\\ c & d \\end{vmatrix}", "abcd"),
        ("\\begin{smallmatrix} a & b \\end{smallmatrix}", "ab"),
    ],
)
def test_matrices_arrays_and_cases_keep_their_cells_and_gain_no_ampersand(tmp_path: Path, body: str, expected: str):
    texts, root, report = omml(tmp_path, display(body))
    assert "".join(texts).replace(" ", "") == expected
    assert list(root.iter(f"{M}m")) and report["math"]["normalized_alignment"] == 0 and report["diagnostics"] == []


def test_alignment_nested_in_a_matrix_cell_and_a_matrix_nested_in_alignment(tmp_path: Path):
    body = "\\begin{aligned} \\begin{array}{cc} p & q \\end{array} &= r \\\\ s &= t \\end{aligned}"
    texts, _, report = omml(tmp_path, display(body))
    assert "&" not in "".join(texts) and "pq" in "".join(texts).replace(" ", "")
    assert report["math"]["normalized_alignment"] == 1


def test_equation_without_alignment_is_left_as_written(tmp_path: Path):
    texts, _, report = omml(tmp_path, display("E = m c^2"))
    assert "".join(texts).replace(" ", "") == "E=mc2" and report["math"]["normalized_alignment"] == 0


def test_numbered_equation_with_aligned_body_loses_label_and_keeps_rows(tmp_path: Path):
    body = (
        "\n\\begin{equation}\n\\begin{aligned}\nx(t) &= A \\sum_{k=1}^{N} \\alpha_k \\left(1-e^{-u}\\right) \\\\\n"
        "&\\quad \\times \\cos\\left(2\\pi f u\\right)H(u)+n(t),\n\\qquad u=t-t_0 .\n\\end{aligned}\n"
        "\\label{eq:synthetic}\n\\end{equation}\n\nSee \\eqref{eq:synthetic}."
    )
    texts, root, report = omml(tmp_path, body)
    assert "&" not in "".join(texts) and "label" not in "".join(texts)
    assert len(list(root.iter(f"{M}mr"))) == 2
    assert [s["text"] for s in report["sequences"]["equations"]] == ["(1)"]


# ------------------------------------------------------------------ explicit failures


def test_stray_alignment_marker_in_an_unknown_environment_fails_explicitly(tmp_path: Path):
    _, report = run_filter(tmp_path, document(display("\\begin{foo} a & b \\end{foo}")))
    assert "L2W-E-MATH-001" in codes(report)


def test_stray_alignment_marker_in_plain_math_fails_explicitly(tmp_path: Path):
    _, report = run_filter(tmp_path, document("Inline $a & b$ here."))
    assert "L2W-E-MATH-001" in codes(report)


@pytest.mark.parametrize("command", ["\\intertext{between}", "\\tag{A}"])
def test_unsupported_equation_commands_fail_explicitly(tmp_path: Path, command: str):
    body = f"\n\\begin{{align*}}\na &= b \\\\ {command}\nc &= d\n\\end{{align*}}\n"
    _, report = run_filter(tmp_path, document(body))
    assert "L2W-E-MATH-003" in codes(report)


def test_two_numbered_rows_in_one_environment_fail_but_one_numbered_row_is_fine(tmp_path: Path):
    two = "\n\\begin{align}\na &= b \\label{e1} \\\\ c &= d \\label{e2}\n\\end{align}\n"
    _, report = run_filter(tmp_path, document(two))
    assert "L2W-E-MATH-003" in codes(report)
    one = "\n\\begin{align}\na &= b \\nonumber \\\\ c &= d \\label{e2}\n\\end{align}\n\nSee \\eqref{e2}."
    texts, _, report = omml(tmp_path, one)
    assert report["diagnostics"] == [] and [s["text"] for s in report["sequences"]["equations"]] == ["(1)"]
    assert "&" not in "".join(texts)


def test_failing_math_is_left_in_place_not_silently_rewritten(tmp_path: Path):
    ast, report = run_filter(tmp_path, document("Inline $a & b$ here."))
    (math,) = [e for e in walk(ast) if e["t"] == "Math"]
    assert math["c"][1] == "a & b" and report["diagnostics"]
