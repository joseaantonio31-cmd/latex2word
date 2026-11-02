"""M4 end to end on fictional fixtures: generic one-/two-column layouts, alternate geometry, metadata, widths, equations.

Each fixture is built with real Pandoc and validated structurally; nothing here is compared with Word renderings.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from layoutkit import DEFAULT_AUTHORS, IEEE_AUTHORS, IEEE_KEYWORDS, FinalDoc, article, build_project, layout_report, problems, write_profile
from pandockit import requires_pandoc

pytestmark = requires_pandoc


def ok(result) -> FinalDoc:
    assert result.ok, problems(result)
    return FinalDoc.open(result.output)


# ---------------------------------------------------------------------------- A. generic one-column article


def test_generic_one_column_article(tmp_path: Path):
    result = build_project(tmp_path, article(), "generic")
    doc = ok(result)
    (section,) = doc.sections()
    assert (section["cols"], section["w"], section["h"]) == (1, 12240, 15840)
    assert section["margins"] == {"top": 1440, "bottom": 1440, "left": 1440, "right": 1440}
    assert doc.texts("Title") == ["A Fictional Layout Study"]
    assert doc.texts("AbstractTitle") == ["Abstract"]  # generic profile: the label is its own paragraph
    assert doc.texts("IndexTerms")[0].startswith("Keywords: layout, fixtures, generic")
    names = [" ".join(" ".join(t.text or "" for t in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")).split()) for p in doc.paragraphs("Author")]
    assert names == ["Ada Example", "Zo\u00eb \u00d6rnek"]
    assert doc.tables("AuthorBlock") == []  # generic layout: stacked paragraphs, not a table
    assert result.validation.metrics["layout_tables"] == 1  # only the equation layout table
    assert layout_report(result)["metadata_counts"] == {"title": 1, "authors": 2, "affiliations": 2, "emails": 2, "abstract": 1, "keywords": 3}
    figure_in = doc.extents_in()
    assert figure_in == pytest.approx([0.98 * 6.5], abs=0.001)  # one column: columnwidth == text width


def test_generic_build_has_no_marker_paragraphs_or_unresolved_content(tmp_path: Path):
    doc = ok(build_project(tmp_path, article(), "generic"))
    text = doc.visible_text()
    assert "IEEE_FRONTMATTER_END" not in text and "??" not in text and "\\cite" not in text
    assert "(1)" in text and "Fig. " not in text  # generic numbering words come from the profile


# ---------------------------------------------------------------------------- B. generic two-column profile


def test_two_column_profile_splits_front_matter_and_body(tmp_path: Path):
    profiles = tmp_path / "profiles"
    write_profile(profiles, "two-column", {"layout": {"columns": {"body": 2, "gap": "0.3in"}}})
    result = build_project(tmp_path / "b", article(), "two-column", profile_dirs=[profiles])
    doc = ok(result)
    front, body = doc.sections()
    assert (front["cols"], body["cols"], body["space"], body["type"]) == (1, 2, 432, "continuous")
    column = (6.5 - 0.3) / 2
    assert doc.extents_in() == pytest.approx([0.98 * column], abs=0.002)
    split = [p for p in doc.body.findall("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p") if p.find(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}sectPr") is not None]
    assert len(split) == 1  # exactly one extra break, carried by a real front-matter paragraph


def test_a_single_column_layout_adds_no_section_break(tmp_path: Path):
    doc = ok(build_project(tmp_path, article(), "generic"))
    assert len(doc.sections()) == 1


# ---------------------------------------------------------------------------- C. alternate page geometry


def test_a4_geometry_in_exact_twips(tmp_path: Path):
    profiles = tmp_path / "profiles"
    write_profile(profiles, "a4-article", {"layout": {"page": {"size": "a4", "margins": {"top": "25mm", "bottom": "20mm", "left": "30mm", "right": "2cm"}}}})
    result = build_project(tmp_path / "c", article(), "a4-article", profile_dirs=[profiles])
    (section,) = ok(result).sections()
    assert (section["w"], section["h"]) == (11906, 16838)
    assert section["margins"] == {"top": 1417, "bottom": 1134, "left": 1701, "right": 1134}
    assert layout_report(result)["layout"]["page"]["text_width_in"] == pytest.approx((210 - 30 - 20) / 25.4, abs=1e-5)


# ---------------------------------------------------------------------------- D/E. metadata and IEEE author blocks


def ieee_authors(count: int) -> str:
    names = ["Ada Example", "Zo\u00eb \u00d6rnek", "Nu\u00f1o Ib\u00e1\u00f1ez", "Bo Sample", "Cy Placeholder", "Di Fictional"]
    blocks = "\n\\and\n".join(
        f"\\IEEEauthorblockN{{{names[i]}}}\n\\IEEEauthorblockA{{Department {i}\\\\Example University\\\\person{i}@example.org}}"
        for i in range(count)
    )
    return f"\\author{{{blocks}}}"


@pytest.mark.parametrize(("count", "rows", "columns"), [(1, 1, 1), (2, 1, 2), (3, 1, 3), (4, 1, 4), (6, 2, 3)])
def test_ieee_author_blocks_render_every_declared_author(tmp_path: Path, count: int, rows: int, columns: int):
    result = build_project(tmp_path, article(author=ieee_authors(count), keywords="\\begin{IEEEkeywords}a, b\\end{IEEEkeywords}"), "ieee-conference")
    doc = ok(result)
    (table,) = doc.tables("AuthorBlock")
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    assert len(table.findall(f"{W}tr")) == rows
    assert len(table.findall(f"{W}tblGrid/{W}gridCol")) == columns
    cells = " ".join(" ".join(t.text or "" for t in tc.iter(f"{W}t")) for tc in table.iter(f"{W}tc"))
    for name in ["Ada Example", "Zo\u00eb \u00d6rnek", "Nu\u00f1o Ib\u00e1\u00f1ez", "Bo Sample", "Cy Placeholder", "Di Fictional"][:count]:
        assert name in cells
    for i in range(count):
        assert f"person{i}@example.org" in cells
    assert layout_report(result)["metadata_counts"]["authors"] == count
    assert doc.texts("IndexTerms")[0].startswith("Index Terms\u2014a, b")
    assert doc.texts("Abstract")[0].startswith("Abstract\u2014")


def test_missing_optional_author_fields_are_fine(tmp_path: Path):
    result = build_project(tmp_path, article(author="\\author{Solo Author}"), "generic")
    doc = ok(result)
    assert layout_report(result)["metadata_counts"]["authors"] == 1 and doc.texts("AuthorEmail") == []


def test_unknown_content_in_the_author_declaration_is_an_error_not_a_silent_loss(tmp_path: Path):
    result = build_project(tmp_path, article(author="\\author{Ada Example\\thanks{Funding text that must not vanish}}"), "generic")
    assert not result.ok and any(f.code.startswith("L2W-E-META") for f in result.findings)


def test_metadata_adapter_is_chosen_by_the_profile_not_the_document_class(tmp_path: Path):
    # An IEEE-style author block under the generic adapter is flagged; under the IEEE adapter it is understood.
    text = article(author=ieee_authors(2), keywords="\\begin{IEEEkeywords}a, b\\end{IEEEkeywords}").replace("\\documentclass{article}", "\\documentclass{IEEEtran}")
    generic = build_project(tmp_path / "g", text, "generic")
    assert not generic.ok
    profiles = tmp_path / "profiles"
    write_profile(profiles, "ieee-like", {"frontmatter": {"metadata_adapter": "ieee-author-blocks", "keywords": {"environments": ["IEEEkeywords"]}}})
    assert build_project(tmp_path / "i", text, "ieee-like", profile_dirs=[profiles]).ok


# ---------------------------------------------------------------------------- F. figure widths


WIDTHS = ("0.5\\textwidth", "0.98\\columnwidth", "3in", "5cm", "40mm", "200pt", "0.5\\linewidth")


def test_figure_widths_resolve_against_the_one_column_geometry(tmp_path: Path):
    result = build_project(tmp_path, article(figures=WIDTHS), "generic")
    ok(result)
    rows = layout_report(result)["figures"]
    assert [r["cx_in"] for r in rows] == pytest.approx([3.25, 6.37, 3.0, 5 / 2.54, 4 / 2.54, 200 / 72, 3.25], abs=0.001)
    assert all(r["svg"] and r["cx_in"] <= r["available_in"] for r in rows)
    assert all(r["cx_in"] / r["cy_in"] == pytest.approx(1.5, rel=0.005) for r in rows)  # the SVG is 240x160: extents keep the aspect


def test_figure_widths_resolve_against_the_two_column_geometry(tmp_path: Path):
    result = build_project(tmp_path, article(author=IEEE_AUTHORS, keywords=IEEE_KEYWORDS, figures=WIDTHS), "ieee-conference")
    ok(result)
    rows = layout_report(result)["figures"]
    column = 3.5
    expected = [column, 0.98 * column, 3.0, 5 / 2.54, 4 / 2.54, 200 / 72, 0.5 * column]
    assert [r["cx_in"] for r in rows] == pytest.approx(expected, abs=0.001)
    clamped = [f for f in result.findings if f.code == "L2W-W-LAYOUT-011"]
    assert len(clamped) == 1 and "0.5\\textwidth" in clamped[0].message.replace(" ", "") or "3.625" in clamped[0].message


def test_resizing_never_regenerates_or_rasterizes_the_vector_asset(tmp_path: Path):
    result = build_project(tmp_path, article(author=IEEE_AUTHORS, keywords=IEEE_KEYWORDS, figures=("0.5\\columnwidth", "3in")), "ieee-conference")
    ok(result)
    import zipfile

    with zipfile.ZipFile(result.final) as archive:
        svgs = [archive.read(n) for n in archive.namelist() if n.endswith(".svg")]
    assert len(svgs) == 1  # one embedded asset, placed twice at two sizes
    assert layout_report(result)["figures"][0]["cx_in"] == pytest.approx(1.75) and layout_report(result)["figures"][1]["cx_in"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------- G. equations


def equations(count: int) -> str:
    items = [
        "\\begin{equation}\nE = mc^2\\label{eq:e0}\n\\end{equation}",
        "\\begin{equation}\n\\sum_{k=1}^{N} \\alpha_k \\left(1 - e^{-t/\\tau_k}\\right) \\cos\\left(2\\pi f_k t + \\phi_k\\right) H(t) + n(t) = x\\label{eq:e1}\n\\end{equation}",
        "\\begin{equation}\n\\begin{aligned}\n a &= b + c \\\\\n  &\\quad - d\n\\end{aligned}\\label{eq:e2}\n\\end{equation}",
    ]
    items += [f"\\begin{{equation}}\nx_{{{i}}} = {i}\\label{{eq:e{i}}}\n\\end{{equation}}" for i in range(3, count)]
    refs = "See Eq.~\\eqref{eq:e0}" + (" and \\eqref{eq:e2}." if count > 2 else ".")
    return "\n\n".join(items[:count]) + "\n\n" + refs


@pytest.mark.parametrize("profile", ["generic", "ieee-conference"])
def test_numbered_equations_use_a_symmetric_layout_table_with_native_omml(tmp_path: Path, profile: str):
    result = build_project(tmp_path, article(author=IEEE_AUTHORS if profile == "ieee-conference" else DEFAULT_AUTHORS, keywords=IEEE_KEYWORDS if profile == "ieee-conference" else "\\keywords{a, b}", equations=equations(12)), profile)
    doc = ok(result)
    tables = doc.tables("EquationLayout")
    assert len(tables) == 12
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
    numbers = []
    for table in tables:
        grid = [int(g.get(f"{W}w")) for g in table.findall(f"{W}tblGrid/{W}gridCol")]
        assert len(grid) == 3 and grid[0] == grid[2] and sum(grid) == int(table.find(f"{W}tblPr/{W}tblW").get(f"{W}w"))
        cells = table.findall(f".//{W}tc")
        assert cells[1].find(f".//{M}oMathPara") is not None  # centred cell holds the equation as native OMML
        numbers.append("".join(t.text or "" for t in cells[2].iter(f"{W}t")))
    assert numbers == [f"({n})" for n in range(1, 13)]
    assert len(doc.tables("Table")) == 1  # layout tables never count as data tables
    assert result.validation.metrics["tables"] == 1
    assert result.validation.metrics["layout_tables"] == 12 + len(doc.tables("AuthorBlock"))
    assert layout_report(result)["equation_layout_mode"] == "table"


def test_equation_references_and_bookmarks_survive_the_layout(tmp_path: Path):
    result = build_project(tmp_path, article(author=IEEE_AUTHORS, keywords=IEEE_KEYWORDS, equations=equations(3)), "ieee-conference")
    doc = ok(result)
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    names = {b.get(f"{W}name") for b in doc.document.iter(f"{W}bookmarkStart")}
    anchors = {h.get(f"{W}anchor") for h in doc.document.iter(f"{W}hyperlink") if h.get(f"{W}anchor")}
    assert anchors and anchors <= names
    links = ["".join(t.text or "" for t in h.iter(f"{W}t")) for h in doc.document.iter(f"{W}hyperlink")]
    assert "(1)" in links and "(3)" in links


def test_the_multiline_aligned_equation_stays_a_native_matrix_without_leaked_ampersands(tmp_path: Path):
    result = build_project(tmp_path, article(author=IEEE_AUTHORS, keywords=IEEE_KEYWORDS, equations=equations(3)), "ieee-conference")
    doc = ok(result)
    M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
    assert len(list(doc.document.iter(f"{M}m"))) == 1  # the aligned environment became one matrix
    assert result.validation.metrics["math_ampersands"] == 0 and result.validation.metrics["eqarr"] == 0


def test_tabs_mode_is_available_for_profiles_that_prefer_it(tmp_path: Path):
    profiles = tmp_path / "profiles"
    write_profile(profiles, "tabbed", {"layout": {"equations": {"mode": "tabs"}}})
    result = build_project(tmp_path / "t", article(equations=equations(2)), "tabbed", profile_dirs=[profiles])
    doc = ok(result)
    assert doc.tables("EquationLayout") == [] and layout_report(result)["equation_layout_mode"] == "tabs"
    first = doc.paragraphs("Equation")[0]
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    assert len(first.findall(f"{W}r/{W}tab")) == 2 and len(first.findall(f"{W}pPr/{W}tabs/{W}tab")) == 2 and " ".join(t.text or "" for t in first.iter(f"{W}t")).strip() == "(1)"
    assert first.find(".//{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath") is not None


# ---------------------------------------------------------------------------- tables, bibliography, acknowledgment


def test_data_tables_stay_real_tables_and_fit_the_column(tmp_path: Path):
    result = build_project(tmp_path, article(author=IEEE_AUTHORS, keywords=IEEE_KEYWORDS), "ieee-conference")
    doc = ok(result)
    (table,) = doc.tables("Table")
    assert layout_report(result)["tables"] == [{"grid_twips": 5040, "tblw_twips": 5040, "available_twips": 5040, "layout": "fixed"}]
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    assert [" ".join(t.text or "" for t in tc.iter(f"{W}t")) for tc in table.iter(f"{W}tc")][:3] == ["Name", "Unit", "Value"]
    assert doc.texts("TableCaption")[0].startswith("TABLE I")


def test_acknowledgment_stays_unnumbered_and_references_keep_their_entries(tmp_path: Path):
    result = build_project(tmp_path, article(author=IEEE_AUTHORS, keywords=IEEE_KEYWORDS), "ieee-conference")
    doc = ok(result)
    headings = doc.texts("Heading1")
    assert headings[0].startswith("I. Introduction") and "Acknowledgment" in headings
    assert result.validation.metrics["bibliography_paragraphs"] == result.summary["counts"]["bibliography_entries_expected"]


# ---------------------------------------------------------------------------- reproducibility, manifest


def test_the_same_inputs_give_the_same_final_bytes_and_a_complete_manifest(tmp_path: Path):
    first = build_project(tmp_path / "one", article(author=IEEE_AUTHORS, keywords=IEEE_KEYWORDS), "ieee-conference")
    second = build_project(tmp_path / "two", article(author=IEEE_AUTHORS, keywords=IEEE_KEYWORDS), "ieee-conference")
    assert first.ok and second.ok
    assert first.final.read_bytes() == second.final.read_bytes()
    import json

    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    for key in ("profile_sha256", "reference_docx", "preliminary_docx", "final_docx", "layout", "metadata"):
        assert manifest[key], key
    assert manifest["final_docx"]["sha256"] == hashlib.sha256(first.final.read_bytes()).hexdigest()
    assert manifest["preliminary_docx"]["sha256"] == hashlib.sha256(first.preliminary.read_bytes()).hexdigest()
    assert manifest["reference_docx"]["sha256"] == json.loads(second.manifest_path.read_text(encoding="utf-8"))["reference_docx"]["sha256"]
    assert manifest["layout"]["sections"][1]["columns"] == 2 and manifest["metadata"]["counts"]["authors"] == 2
    text = first.manifest_path.read_text(encoding="utf-8").replace("\\\\", "/")
    assert tmp_path.as_posix() not in text  # the manifest is portable: no workspace or project locations
