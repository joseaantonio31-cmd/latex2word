"""M3 integration on an external reference project (deselected by default; run with ``-m integration``).

Environment (read-only inputs); the tests skip when one is missing:

* ``LATEX2WORD_INTEGRATION_PAPER_ROOT``    - root of the reference LaTeX project;
* ``LATEX2WORD_INTEGRATION_EXPECTATIONS``  - local JSON file with the project's expectations (``integration_project``);
* ``LATEX2WORD_INTEGRATION_MAIN``          - main file of the variant with an external ``.bib`` (citeproc) and an
  unnumbered heading: the primary semantic source, because only it shows the unnumbered-heading result *and* the
  ``.bib`` bibliography result together;
* ``LATEX2WORD_INTEGRATION_MAIN_INLINE``   - main file of the variant with an inline ``thebibliography`` (manual
  mode); it must reach identical numbering;
* ``LATEX2WORD_INTEGRATION_CSL``           - the CSL style of the reference project's bibliography;
* ``LATEX2WORD_BASELINE_ROOT``             - an earlier reference conversion (its ``pristine_output`` DOCX);
* ``LATEX2WORD_INTEGRATION_M3_WORKSPACE``  - optional directory that receives one kept workspace per variant.

The project is never modified; the long-term target is one content source plus profile/config variants.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest
import yaml
from integration_project import expect
from projectkit import tree_digest

from latex2word.build import BuildResult, run_build
from latex2word.config import load_config
from latex2word.diagnostics import Severity

pytestmark = pytest.mark.integration

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
ASVG = "{http://schemas.microsoft.com/office/drawing/2016/SVG/main}"
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}


def _env_path(name: str, *, directory: bool = True) -> Path:
    value = os.environ.get(name)
    path = Path(value) if value else None
    if path is None or not (path.is_dir() if directory else path.is_file()):
        pytest.skip(f"{name} is not set to an existing {'directory' if directory else 'file'}")
    return path


@pytest.fixture(scope="module")
def paper_root() -> Path:
    return _env_path("LATEX2WORD_INTEGRATION_PAPER_ROOT")


@pytest.fixture(scope="module")
def baseline_docx() -> Path:
    root = _env_path("LATEX2WORD_BASELINE_ROOT")
    pristine = sorted(p for p in root.rglob("*.docx") if p.parent.name == "pristine_output")
    if len(pristine) != 1:
        pytest.skip(f"expected exactly one DOCX in a pristine_output directory of the baseline, found {len(pristine)}")
    return pristine[0]


@pytest.fixture(scope="module")
def csl() -> Path:
    return _env_path("LATEX2WORD_INTEGRATION_CSL", directory=False)


def semantic_snapshot(path: Path, heading: str | None = None) -> dict[str, Any]:
    """The semantic facts of a DOCX, extracted identically for the baseline and the M3 output.

    ``heading``: text of an unnumbered heading to report as ``named_heading`` (with any number prefix it got).
    """
    archive = zipfile.ZipFile(path)
    root = ET.fromstring(archive.read("word/document.xml"))
    paragraphs = list(root.iter(f"{W}p"))

    def style(p: ET.Element) -> str:
        node = p.find(f"{W}pPr/{W}pStyle")
        return node.get(f"{W}val", "") if node is not None else ""

    def text(p: ET.Element) -> str:
        return "".join(t.text or "" for t in p.iter(f"{W}t"))

    def math_text(p: ET.Element) -> str:
        return "".join(t.text or "" for t in p.iter(f"{M}t"))

    figures = [int(m.group(1)) for p in paragraphs if style(p) == "ImageCaption" and (m := re.match(r"Fig\.\s*(\d+)\.", text(p)))]
    tables = [ROMAN[m.group(1)] for p in paragraphs if style(p) == "TableCaption" and (m := re.match(r"TABLE\s*([IVX]+)", text(p)))]
    equations: list[int] = []
    for index, p in enumerate(paragraphs):
        if p.find(f".//{M}oMathPara") is None:
            continue
        label = re.search(r"\((\d+)\)\s*$", math_text(p) + text(p))
        if label is None and index + 1 < len(paragraphs) and style(paragraphs[index + 1]) == "EquationNumber":
            label = re.fullmatch(r"\((\d+)\)", text(paragraphs[index + 1]).strip())
        if label:
            equations.append(int(label.group(1)))
    # bookmarks that mark an equation: inside the equation paragraph (wherever it sits, also inside a layout
    # table cell), or at body level right before it
    equation_bookmarks: set[str] = set()
    pending: list[str] = []
    for child in root.find(f"{W}body"):
        if child.tag == f"{W}bookmarkStart":
            pending.append(child.get(f"{W}name", ""))
        elif child.tag == f"{W}p":
            names = pending + [b.get(f"{W}name", "") for b in child.iter(f"{W}bookmarkStart")]
            if child.find(f".//{M}oMathPara") is not None:
                equation_bookmarks.update(names)
            pending = []
    for p in paragraphs:
        if p.find(f".//{M}oMathPara") is not None:
            equation_bookmarks.update(b.get(f"{W}name", "") for b in p.iter(f"{W}bookmarkStart"))
    link_texts = [
        (h.get(f"{W}anchor"), "".join(t.text or "" for t in h.iter(f"{W}t"))) for h in root.iter(f"{W}hyperlink") if h.get(f"{W}anchor")
    ]
    equation_refs = [t for anchor, t in link_texts if anchor in equation_bookmarks]
    headings = [(style(p), text(p)) for p in paragraphs if style(p).startswith("Heading")]
    visible = "".join(text(p) + "\n" for p in paragraphs)
    ampersands_in_equation_arrays = sum((t.text or "").count("&") for arr in root.iter(f"{M}eqArr") for t in arr.iter(f"{M}t"))
    drawings = list(root.iter(f"{WP}inline"))
    extents = [(int(d.find(f"{WP}extent").get("cx")), int(d.find(f"{WP}extent").get("cy"))) for d in drawings]
    return {
        "figures": figures,
        "tables": tables,
        "equations": equations,
        "equation_refs": equation_refs,
        "equation_refs_parenthesised": bool(equation_refs) and all(re.fullmatch(r"\(\d+\)", t) for t in equation_refs),
        "ampersands_in_equation_arrays": ampersands_in_equation_arrays,
        "named_heading": next((t for s, t in headings if heading and heading in t), None),
        "raw_latex": len(re.findall(r"\\(?:cite|ref|eqref|input)\b", visible + "".join(math_text(p) for p in paragraphs))),
        "unresolved": visible.count("??"),
        "svg_drawings": sum(1 for _ in root.iter(f"{ASVG}svgBlip")),
        "drawings": len(drawings),
        "extents": extents,
        "bibliography_paragraphs": sum(1 for p in paragraphs if style(p) == "Bibliography"),
    }


def unnumbered_heading() -> str:
    return expect("numbering", "unnumbered_headings")[0]


def numbers(labels: list[str]) -> list[int]:
    """1-based positions of numbering labels such as ``["Fig. 1.", "Fig. 2."]`` (consecutive by construction)."""
    return list(range(1, len(labels) + 1))


def _env_main(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set")
    return value


def build_paper(tmp_path_factory, paper_root: Path, main: str, csl: Path | None) -> tuple[Path, BuildResult, dict[str, str], dict[str, str]]:
    base = tmp_path_factory.mktemp(f"m3-{Path(main).stem}")
    shared = os.environ.get("LATEX2WORD_INTEGRATION_M3_WORKSPACE")
    workspace = Path(shared) / Path(main).stem if shared else base / "workspace"
    data: dict[str, Any] = {
        "schema_version": 1,
        "project": {"name": "integration-reference", "root": str(paper_root), "main": main},
        "profile": "ieee-conference",
        "workspace": {"root": str(workspace)},
        "output": {"directory": str(base / "out")},
        "figures": {"patches": expect("figure_patches")},
    }
    if csl is not None:
        data["bibliography"] = {"csl": str(csl)}
    config_path = base / "config.yaml"
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)
    before = tree_digest(paper_root)
    result = run_build(config)
    after = tree_digest(paper_root)
    return base, result, before, after


@pytest.fixture(scope="module")
def citeproc_build(paper_root: Path, csl: Path, tmp_path_factory):
    return build_paper(tmp_path_factory, paper_root, _env_main("LATEX2WORD_INTEGRATION_MAIN"), csl)


@pytest.fixture(scope="module")
def manual_build(paper_root: Path, tmp_path_factory):
    return build_paper(tmp_path_factory, paper_root, _env_main("LATEX2WORD_INTEGRATION_MAIN_INLINE"), None)


def test_the_reference_paper_builds_and_the_source_tree_is_byte_identical(citeproc_build):
    _, result, before, after = citeproc_build
    assert result.ok, [str(f) for f in result.findings if f.severity is not Severity.INFO]
    assert before == after


def test_semantic_numbering_of_the_reference_paper(citeproc_build):
    _, result, _, _ = citeproc_build
    numbering = result.summary["numbering"]
    for key in ("figures", "tables", "equations"):
        assert numbering[key] == expect("numbering", key), key
    assert [s["text"] for s in numbering["sections"]] == expect("numbering", "sections")
    assert numbering["unnumbered_headings"] == expect("numbering", "unnumbered_headings")
    assert result.summary["counts"]["active_figure_assets"] == expect("counts", "active_figure_assets")


def test_references_citations_and_bibliography_resolve(citeproc_build):
    _, result, _, _ = citeproc_build
    snapshot = semantic_snapshot(result.output)
    entries = expect("counts", "bibliography_entries")
    assert snapshot["bibliography_paragraphs"] == entries and result.summary["counts"]["bibliography_entries_expected"] == entries
    assert snapshot["raw_latex"] == 0 and snapshot["unresolved"] == 0
    assert snapshot["equation_refs_parenthesised"] and sorted(set(snapshot["equation_refs"])) == expect("numbering", "equations")
    assert result.validation.metrics["internal_hyperlinks"] > 0 and not [f for f in result.errors]


def test_no_alignment_ampersand_survives_in_aligned_equations(citeproc_build):
    _, result, _, _ = citeproc_build
    assert semantic_snapshot(result.output)["ampersands_in_equation_arrays"] == 0
    assert result.validation.metrics["eqarr"] == 0 and result.validation.metrics["math_ampersands"] == 0
    assert result.summary["counts"]["math_alignment_environments_normalised"] == expect("counts", "math_alignment_environments_normalised")


def test_all_figures_are_vectors_with_consistent_physical_sizes(citeproc_build):
    _, result, _, _ = citeproc_build
    snapshot = semantic_snapshot(result.output)
    assert snapshot["drawings"] == snapshot["svg_drawings"] == expect("counts", "active_figure_assets")
    figures = result.validation.metrics["figures"]
    widths = [f["cx_in"] for f in figures]
    assert max(widths) - min(widths) <= 0.01 * max(widths)  # all requested with the same width expression
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert {f["width"]["original"] for f in manifest["figures"]} == {expect("figures", "width_original")}
    assert {f["width"]["emitted"] for f in manifest["figures"]} == {expect("figures", "width_emitted")}
    for figure in figures:
        assert figure["aspect"] == pytest.approx(next(f["aspect"] for f in manifest["figures"] if f["id"] == figure["id"]), rel=0.01)


def test_the_inline_bibliography_variant_reaches_the_same_numbering(manual_build, citeproc_build):
    _, manual, before, after = manual_build
    _, citeproc, _, _ = citeproc_build
    assert manual.ok, [str(f) for f in manual.findings if f.severity is not Severity.INFO] and before == after
    assert manual.summary["bibliography_mode"] == "manual" and citeproc.summary["bibliography_mode"] == "citeproc"
    for key in ("figures", "tables", "equations"):
        assert manual.summary["numbering"][key] == citeproc.summary["numbering"][key]
    assert semantic_snapshot(manual.output)["bibliography_paragraphs"] == expect("counts", "bibliography_entries")


def test_comparison_with_the_baseline(citeproc_build, baseline_docx: Path):
    base, result, _, _ = citeproc_build
    heading = unnumbered_heading()
    before, after = semantic_snapshot(baseline_docx, heading), semantic_snapshot(result.output, heading)
    for key in ("figures", "tables", "equations"):
        assert before[key] == expect("baseline", key), key
        assert after[key] == numbers(expect("numbering", key)), key
    assert not before["equation_refs_parenthesised"] and after["equation_refs_parenthesised"]
    assert before["ampersands_in_equation_arrays"] > 0 and after["ampersands_in_equation_arrays"] == 0
    assert re.match(rf"^[IVX]+\.\s*{re.escape(heading)}", before["named_heading"] or "")
    assert after["named_heading"] == heading
    figures = expect("counts", "active_figure_assets")

    def verdict(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    rows = [
        ("Figures", ",".join(map(str, before["figures"])), ",".join(map(str, after["figures"]))),
        ("Tables", ",".join("I II III IV V".split()[n - 1] for n in before["tables"]), ",".join("I II III IV V".split()[n - 1] for n in after["tables"])),
        ("Equations", ",".join(map(str, before["equations"])), ",".join(map(str, after["equations"]))),
        ("eqref parentheses", verdict(before["equation_refs_parenthesised"]), verdict(after["equation_refs_parenthesised"])),
        ("math alignment ampersand", verdict(before["ampersands_in_equation_arrays"] == 0), verdict(after["ampersands_in_equation_arrays"] == 0)),
        ("Unnumbered heading", "FAIL", verdict(after["named_heading"] == heading)),
        ("raw LaTeX", verdict(before["raw_latex"] == 0), verdict(after["raw_latex"] == 0)),
        ("unresolved refs", verdict(before["unresolved"] == 0), verdict(after["unresolved"] == 0)),
        ("figures vector", verdict(before["svg_drawings"] == before["drawings"] == figures), verdict(after["svg_drawings"] == after["drawings"] == figures)),
    ]
    table = "\n".join(f"{name:<28}{old:<14}{new}" for name, old, new in [("", "Baseline", "M3"), *rows])
    report = Path(result.workspace) / "report"
    report.mkdir(exist_ok=True)
    (report / "m3_semantic_comparison.md").write_text("```\n" + table + "\n```\n", encoding="utf-8")
    (report / "m3_semantic_comparison.json").write_text(json.dumps({"baseline": before, "m3": after}, indent=2, default=list), encoding="utf-8")
    assert all(new == "PASS" for name, _, new in rows if name not in ("Figures", "Tables", "Equations"))
