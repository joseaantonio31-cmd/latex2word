"""M4 integration on the external reference project (deselected by default; run with ``-m integration``).

Environment (read-only inputs; the tests skip when one is missing), as for M3:

* ``LATEX2WORD_INTEGRATION_PAPER_ROOT``    - root of the reference LaTeX project;
* ``LATEX2WORD_INTEGRATION_EXPECTATIONS``  - local JSON file with the project's expectations (``integration_project``);
* ``LATEX2WORD_INTEGRATION_MAIN``          - main file of the variant with an external ``.bib`` and an unnumbered heading;
* ``LATEX2WORD_INTEGRATION_CSL``           - the CSL style of the reference project's bibliography;
* ``LATEX2WORD_BASELINE_ROOT``             - an earlier reference conversion (comparison evidence only);
* ``LATEX2WORD_INTEGRATION_M4_WORKSPACE``  - optional directory that keeps the workspace (preliminary.docx, final.docx,
  build_manifest.json, layout_report.json, visual_review.md and the baseline comparison).

Nothing about the project (author names, e-mail addresses, institutions, figure names, counts) is written here:
counts and names come from the local expectations, author facts from the source files at run time, and geometry
from the tracked ``ieee-conference`` profile.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from integration_project import expect
from lxml import etree
from projectkit import tree_digest
from test_integration_m3 import numbers, semantic_snapshot, unnumbered_heading

from latex2word.build import BuildResult, run_build
from latex2word.config import load_config
from latex2word.diagnostics import Severity
from latex2word.ooxml.namespaces import qn, text_of, w

pytestmark = pytest.mark.integration


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
def main_name() -> str:
    value = os.environ.get("LATEX2WORD_INTEGRATION_MAIN")
    if not value:
        pytest.skip("LATEX2WORD_INTEGRATION_MAIN is not set")
    return value


@pytest.fixture(scope="module")
def baseline_docx() -> Path:
    root = _env_path("LATEX2WORD_BASELINE_ROOT")
    found = sorted(p for p in root.rglob("*.docx") if p.parent.name == "pristine_output")
    if len(found) != 1:
        pytest.skip("expected exactly one DOCX in a pristine_output directory of the baseline")
    return found[0]


def project_config(paper_root: Path, main_name: str, workspace: Path, out: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project": {"name": "integration-reference", "root": str(paper_root), "main": main_name},
        "profile": "ieee-conference",
        "workspace": {"root": str(workspace)},
        "output": {"directory": str(out)},
        "figures": {"patches": expect("figure_patches")},
        "bibliography": {"csl": str(_env_path("LATEX2WORD_INTEGRATION_CSL", directory=False))},
    }


@pytest.fixture(scope="module")
def build(paper_root: Path, main_name: str, tmp_path_factory) -> tuple[BuildResult, dict[str, str], dict[str, str]]:
    base = tmp_path_factory.mktemp("m4")
    shared = os.environ.get("LATEX2WORD_INTEGRATION_M4_WORKSPACE")
    workspace = Path(shared) / Path(main_name).stem if shared else base / "workspace"
    data = project_config(paper_root, main_name, workspace, base / "out")
    config_path = base / "config.yaml"
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    before = tree_digest(paper_root)
    result = run_build(load_config(config_path))
    return result, before, tree_digest(paper_root)


def read_part(path: Path, name: str) -> etree._Element:
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read(name))


def source_declarations(paper_root: Path, main_name: str) -> dict[str, list[str]]:
    """What the source itself declares about its authors (names, e-mail addresses), read from the preamble."""
    text = (paper_root / main_name).read_text(encoding="utf-8")
    preamble = text.split("\\begin{document}", 1)[0]
    declared = preamble[preamble.index("\\author") :] if "\\author" in preamble else ""
    return {
        "emails": re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", declared),
        "names": [n.strip() for n in re.findall(r"\\normalsize\s+([^}]+)\}", declared)]
        or [n.strip() for n in re.findall(r"\\IEEEauthorblockN\{([^}]+)\}", declared)],
        "blocks": [len(re.findall(r"\\parbox", declared)) or len(re.findall(r"\\IEEEauthorblockN", declared))],
    }


# ------------------------------------------------------------------------------------ build and source integrity


def test_the_reference_paper_builds_through_the_whole_m4_pipeline_and_the_source_is_untouched(build):
    result, before, after = build
    assert result.ok, [str(f) for f in result.findings if f.severity is not Severity.INFO]
    assert before == after
    assert result.preliminary.is_file() and result.final.is_file() and result.layout_report.is_file()
    assert result.visual_review.is_file() and result.manifest_path.is_file()
    assert not [f for f in result.findings if f.code in ("L2W-W-META-001", "L2W-E-META-001")]
    assert not [f for f in result.findings if f.code.startswith(("L2W-E-TEX", "L2W-W-TEX-001"))]


def test_m3_semantics_are_preserved_in_the_final_document(build):
    result, _, _ = build
    numbering = result.summary["numbering"]
    for key in ("figures", "tables", "equations"):
        assert numbering[key] == expect("numbering", key), key
    assert [s["text"] for s in numbering["sections"]] == expect("numbering", "sections")
    assert numbering["unnumbered_headings"] == expect("numbering", "unnumbered_headings")
    counts = result.summary["counts"]
    for key in ("active_figure_assets", "math_display", "math_inline"):
        assert counts[key] == expect("counts", key), key
    heading = unnumbered_heading()
    snapshot = semantic_snapshot(result.final, heading)
    for key in ("figures", "tables", "equations"):
        assert snapshot[key] == numbers(expect("numbering", key)), key
    assert snapshot["equation_refs_parenthesised"] and sorted(set(snapshot["equation_refs"])) == expect("numbering", "equations")
    assert snapshot["raw_latex"] == 0 and snapshot["unresolved"] == 0
    assert snapshot["svg_drawings"] == snapshot["drawings"] == expect("counts", "active_figure_assets")
    assert snapshot["bibliography_paragraphs"] == expect("counts", "bibliography_entries")
    assert snapshot["ampersands_in_equation_arrays"] == 0 and snapshot["named_heading"] == heading
    metrics = result.validation.metrics
    assert metrics["math_ampersands"] == 0 and metrics["eqarr"] == 0
    assert metrics["omml_display"] == expect("counts", "math_display") and metrics["omml_inline"] == expect("counts", "math_inline")


# ------------------------------------------------------------------------------------ geometry and sections (profile)


def test_page_geometry_sections_and_columns_match_the_profile(build):
    result, _, _ = build
    document = read_part(result.final, "word/document.xml")
    sections = list(document.iter(w("sectPr")))
    assert len(sections) == 2
    for sect in sections:
        size, margin = sect.find(w("pgSz")), sect.find(w("pgMar"))
        assert (int(size.get(w("w"))), int(size.get(w("h")))) == (12240, 15840)
        assert [int(margin.get(w(k))) for k in ("top", "bottom", "left", "right")] == [1080, 1440, 900, 900]
    front, body = sections
    assert (front.find(w("cols")).get(w("num")), body.find(w("cols")).get(w("num"))) == ("1", "2")
    assert body.find(w("cols")).get(w("space")) == "360" and body.find(w("type")).get(w("val")) == "continuous"
    carrier = next(p for p in document.iter(w("p")) if p.find(f"{w('pPr')}/{w('sectPr')}") is not None)
    following = next(c for c in carrier.itersiblings() if c.tag == w("p"))
    assert following.find(f"{w('pPr')}/{w('pStyle')}").get(w("val")) == "Heading1" and text_of(following).startswith("I. ")


def test_front_matter_is_generated_from_the_source_declarations(build, paper_root: Path, main_name: str):
    result, _, _ = build
    document = read_part(result.final, "word/document.xml")
    declared = source_declarations(paper_root, main_name)
    assert declared["emails"] and declared["names"]
    visible = "\n".join(text_of(p) for p in document.iter(w("p")))
    for email in declared["emails"]:
        assert email in visible
    for name in declared["names"]:
        assert name in visible  # Unicode accents exactly as written in the source
    layout = json.loads(result.layout_report.read_text(encoding="utf-8"))
    assert layout["metadata_counts"]["authors"] == declared["blocks"][0] == len(declared["names"])
    assert layout["frontmatter"]["title_present"] and layout["frontmatter"]["abstract_present"] and layout["frontmatter"]["keywords_present"]
    styles = {p.find(f"{w('pPr')}/{w('pStyle')}").get(w("val")): text_of(p) for p in document.iter(w("p")) if p.find(f"{w('pPr')}/{w('pStyle')}") is not None}
    assert styles["Abstract"].startswith("Abstract\u2014") and styles["IndexTerms"].startswith("Index Terms\u2014")
    assert "IEEE_FRONTMATTER_END" not in visible
    assert len([t for t in document.iter(w("tbl")) if t.find(f"{w('tblPr')}/{w('tblStyle')}").get(w("val")) == "AuthorBlock"]) == 1


# ------------------------------------------------------------------------------------ figures, tables, equations


def test_figures_are_sized_against_the_final_column_width(build):
    result, _, _ = build
    document = read_part(result.final, "word/document.xml")
    extents = [int(e.get("cx")) for e in document.iter("{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}extent")]
    figures, fraction = expect("counts", "active_figure_assets"), expect("figures", "width_fraction")
    column_in = (7.25 - 0.25) / 2  # the ieee-conference profile: 7.25 in text width, 0.25 in gap, two columns
    expected = round(fraction * column_in * 914400)
    assert len(extents) == figures and all(abs(cx - expected) <= 635 for cx in extents)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert {f["width"]["original"] for f in manifest["figures"]} == {expect("figures", "width_original")}
    for row in manifest["layout"]["figures"]:
        assert row["svg"] and row["cx_in"] == pytest.approx(fraction * column_in, abs=0.001) and row["cx_in"] <= row["available_in"] == column_in
    with zipfile.ZipFile(result.final) as archive:
        assert len([n for n in archive.namelist() if n.endswith(".svg")]) == figures
        assert len([n for n in archive.namelist() if n.endswith(".png")]) == figures


def test_tables_fit_the_column_and_stay_real_word_tables(build):
    result, _, _ = build
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    tables = manifest["layout"]["tables"]
    count = expect("counts", "data_tables")
    assert len(tables) == count and all(t["grid_twips"] <= t["available_twips"] == 5040 for t in tables)
    assert result.validation.metrics["tables"] == count


def test_numbered_equations_are_native_omml_in_symmetric_layout_tables(build):
    result, _, _ = build
    document = read_part(result.final, "word/document.xml")
    layout_tables = [t for t in document.iter(w("tbl")) if t.find(f"{w('tblPr')}/{w('tblStyle')}").get(w("val")) == "EquationLayout"]
    expected = expect("numbering", "equations")
    assert len(layout_tables) == len(expected)
    found = []
    for table in layout_tables:
        cells = table.findall(f".//{w('tc')}")
        grid = [int(g.get(w("w"))) for g in table.findall(f"{w('tblGrid')}/{w('gridCol')}")]
        assert grid[0] == grid[2] and cells[1].find(f".//{qn('m:oMathPara')}") is not None
        found.append(text_of(cells[2]).strip())
    assert found == expected
    # a normalised multi-line aligned equation is one native matrix
    assert len(list(document.iter(qn("m:m")))) == expect("counts", "native_matrices")


def test_bibliography_and_unnumbered_heading(build):
    result, _, _ = build
    document = read_part(result.final, "word/document.xml")
    headings = [text_of(p) for p in document.iter(w("p")) if (s := p.find(f"{w('pPr')}/{w('pStyle')}")) is not None and s.get(w("val")) == "Heading1"]
    assert headings[-2:] == [unnumbered_heading(), "References"] and not re.match(r"^[IVX]+\.", headings[-2])
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["layout"]["styles"]["Bibliography"]["size_pt"] == 8.0


# ------------------------------------------------------------------------------------ manifest and reproducibility


def test_manifest_records_the_layout_pass(build):
    result, _, _ = build
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["profile_sha256"]) == 64 and manifest["reference_docx"]["mode"] == "generated"
    assert manifest["preliminary_docx"]["sha256"] != manifest["final_docx"]["sha256"]
    assert manifest["final_docx"]["sha256"] == result.summary["output_sha256"]
    assert {"word/document.xml", "word/styles.xml"} <= set(manifest["final_docx"]["part_hashes"])
    assert manifest["layout"]["equation_layout_mode"] == "table"
    assert manifest["metadata"]["counts"]["authors"] == expect("counts", "authors")


def test_rebuilding_gives_the_same_final_document(build, paper_root: Path, main_name: str, tmp_path: Path):
    first, _, _ = build
    data = project_config(paper_root, main_name, tmp_path / "workspace", tmp_path / "out")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    second = run_build(load_config(tmp_path / "config.yaml"))
    assert second.ok
    assert second.final.read_bytes() == first.final.read_bytes()


# ------------------------------------------------------------------------------------ baseline comparison


def geometry_of(path: Path) -> dict[str, Any]:
    document = read_part(path, "word/document.xml")
    last = list(document.iter(w("sectPr")))[-1]
    size, margin, cols = last.find(w("pgSz")), last.find(w("pgMar")), last.find(w("cols"))
    return {
        "page": (int(size.get(w("w"))), int(size.get(w("h")))),
        "margins": tuple(int(margin.get(w(k))) for k in ("top", "bottom", "left", "right")),
        "columns": int(cols.get(w("num"))), "gap": int(cols.get(w("space"))),
        "sections": len(list(document.iter(w("sectPr")))),
        "figure_widths_in": [round(int(e.get("cx")) / 914400, 3) for e in document.iter("{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}extent")],
    }


def test_comparison_with_the_baseline_is_written_to_the_workspace(build, baseline_docx: Path):
    result, _, _ = build
    before, after = geometry_of(baseline_docx), geometry_of(result.final)
    # the baseline got the page geometry, the columns and the gap right; M4 reproduces them from profile data
    for key in ("page", "margins", "columns", "gap", "sections"):
        assert before[key] == after[key], key
    heading = unnumbered_heading()
    old, new = semantic_snapshot(baseline_docx, heading), semantic_snapshot(result.final, heading)
    # the baseline's numbering bugs are not inherited
    assert old["figures"] != new["figures"] and new["figures"] == numbers(expect("numbering", "figures"))
    assert re.match(rf"^[IVX]+\.\s*{re.escape(heading)}", old["named_heading"] or "") and new["named_heading"] == heading
    assert old["bibliography_paragraphs"] == expect("baseline", "bibliography_paragraphs")
    rows = [
        ("Geometry (page, margins, twips)", f"{before['page']} {before['margins']}", f"{after['page']} {after['margins']}"),
        ("Columns / gap / sections", f"{before['columns']} / {before['gap']} / {before['sections']}", f"{after['columns']} / {after['gap']} / {after['sections']}"),
        ("Figure widths (in)", str(before["figure_widths_in"]), str(after["figure_widths_in"])),
        ("Equations", "semantics FAIL (ampersands leaked, refs unparenthesised)", "semantics PASS, number right-aligned in a symmetric layout table"),
        ("Unnumbered heading", "numbered (wrong)", "unnumbered"),
        ("Author block", "manually edited Word block (not a source of truth)", "generated from the source declarations"),
    ]
    report = Path(result.workspace) / "report"
    report.mkdir(exist_ok=True)
    table = "\n".join(f"{name:<34}{old_value:<62}{new_value}" for name, old_value, new_value in [("", "Baseline", "M4"), *rows])
    (report / "m4_baseline_comparison.md").write_text("```\n" + table + "\n```\n", encoding="utf-8")
