"""``latex2word build`` end to end on deterministic fixtures (real Pandoc, SVG-only figures)."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT, fake_which
from pandockit import requires_pandoc
from projectkit import tree_digest

from latex2word.build import BuildResult, run_build
from latex2word.pandoc_adapter import locate_pandoc
from latex2word.config import load_config
from latex2word.diagnostics import Severity
from latex2word.errors import BuildError, ConfigError

pytestmark = requires_pandoc

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "semantic"


def setup_project(tmp_path: Path, fixture: str, *, profile: str = "generic", main: str = "main.tex", extra: dict | None = None,
                  profile_dirs: bool = False, edit: dict[str, str] | None = None):
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / fixture, project)
    for rel, text in (edit or {}).items():
        (project / rel).write_text(text, encoding="utf-8")
    data = {
        "schema_version": 1,
        "project": {"name": fixture, "root": str(project), "main": main},
        "profile": profile,
        "workspace": {"root": str(tmp_path / "workspace")},
        "output": {"directory": str(tmp_path / "out")},
        "tools": {"timeout_s": 60},
        **(extra or {}),
    }
    if profile_dirs:
        shutil.copytree(FIXTURES / "profiles", tmp_path / "profiles")
        data["profile_dirs"] = [str(tmp_path / "profiles")]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return project, load_config(config_path)


def build(tmp_path: Path, fixture: str, **kwargs) -> tuple[Path, BuildResult]:
    project, config = setup_project(tmp_path, fixture, **kwargs)
    return project, run_build(config)


def docx_text(path: Path) -> str:
    import re

    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml).replace("&amp;", "&")


def error_codes(result: BuildResult) -> list[str]:
    return [f.code for f in result.findings if f.severity is Severity.ERROR]


# --------------------------------------------------------------------- fixture A: generic article


def test_generic_article_builds_with_generic_numbering_and_resolved_references(tmp_path: Path):
    project, result = build(tmp_path, "generic_article")
    assert result.ok, [str(f) for f in result.findings if f.severity is not Severity.INFO]
    numbering = result.summary["numbering"]
    assert [s["text"] for s in numbering["sections"]] == ["1", "2", "2.1", "2.2", "3"]
    assert numbering["unnumbered_headings"] == ["Data Availability", "Supplementary Material"]
    assert numbering["figures"] == ["Figure 1", "Figure 2"] and numbering["tables"] == ["Table 1", "Table 2"]
    assert numbering["equations"] == ["(1)", "(2)"]
    counts = result.summary["counts"]
    assert counts["references"] == 8 and counts["citation_keys"] == 3 and counts["bibliography_entries_expected"] == 3
    text = docx_text(result.output)
    for expected in ("1 Introduction", "Data Availability", "Figure 1: The first example.", "Table 2: Parameters of the second run.",
                     "Eq. (1)", "energy relation in (2)", "Section 2 and the setup in Section 2.1", "Table 2 lists more"):
        assert expected in text.replace("\u00a0", " "), expected
    for raw in ("\\cite", "\\ref", "\\eqref", "\\label", "??"):
        assert raw not in text
    assert "2 Data Availability" not in text and "4 Data Availability" not in text
    assert result.validation.metrics["bibliography_paragraphs"] == 3 and result.validation.metrics["eqarr"] == 0


def test_the_project_tree_is_untouched_and_only_the_workspace_and_output_are_written(tmp_path: Path):
    project, config = setup_project(tmp_path, "generic_article")
    before = tree_digest(project)
    result = run_build(config)
    assert result.ok and tree_digest(project) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.yaml", "out", "project", "workspace"]
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["main.docx"]


def test_build_manifest_records_versions_hashes_transformations_and_semantics(tmp_path: Path):
    project, result = build(tmp_path, "generic_article")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "latex2word.build_manifest/1" and manifest["status"] == "ok"
    # the version of the Pandoc that actually ran (certified or not: see test_pandoc_compat.py)
    assert manifest["pandoc_version"] == locate_pandoc().version and manifest["latex2word_version"]
    assert manifest["profile"] == {"id": "generic", "chain": ["generic"], "display_name": "Generic academic article"}
    assert manifest["source"]["main"] == "main.tex" and set(manifest["source"]["file_hashes"]) >= {"main.tex", "refs.bib"}
    assert manifest["source"]["file_hashes"]["main.tex"] == hashlib.sha256((project / "main.tex").read_bytes()).hexdigest()
    ws = Path(result.workspace)
    assert manifest["staging_manifest_sha256"] == hashlib.sha256((ws / "staging_manifest.json").read_bytes()).hexdigest()
    assert manifest["figure_manifest_sha256"] == hashlib.sha256((ws / "figures" / "figure_manifest.json").read_bytes()).hexdigest()
    assert manifest["output"]["sha256"] == hashlib.sha256(result.output.read_bytes()).hexdigest()
    assert [f["name"] for f in manifest["filters"]] == ["l2w_semantic.lua", "l2w_math.lua", "l2w_util.lua"]
    # reference-marker: the per-occurrence markers of F-04 (L2W-E-REF-012) are staged-copy edits too
    assert {t["kind"] for t in manifest["transformations"]} == {"figure-width", "bibliography-remove", "reference-marker"}
    assert manifest["numbering"]["figures"] == ["Figure 1", "Figure 2"]
    assert manifest["semantic_counts"]["equations"] == 2 and manifest["bibliography"]["mode"] == "citeproc"
    assert "word/document.xml" in manifest["docx_part_hashes"] and manifest["docx_metrics"]["drawings"] == 2
    text = result.manifest_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in text and tmp_path.as_posix() not in text, "no absolute machine paths in the manifest"
    assert manifest["pandoc"]["exit_code"] == 0 and Path(manifest["pandoc"]["argv"][0]).stem.lower() == "pandoc"


def test_builds_are_reproducible_part_by_part(tmp_path: Path):
    _, first = build(tmp_path / "a", "generic_article")
    _, second = build(tmp_path / "b", "generic_article")
    m1 = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    m2 = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert m1["docx_part_hashes"] == m2["docx_part_hashes"] and m1["output"]["sha256"] == m2["output"]["sha256"]


# ------------------------------------------------------------- fixture B: alternate profile


def test_same_source_other_profile_gives_other_semantics_without_code_changes(tmp_path: Path):
    _, generic = build(tmp_path / "g", "generic_article")
    _, alt = build(tmp_path / "a", "generic_article", profile="alt-numbering", profile_dirs=True)
    assert generic.ok and alt.ok
    g, a = generic.summary["numbering"], alt.summary["numbering"]
    assert a["figures"] == ["Plate A", "Plate B"] and g["figures"] == ["Figure 1", "Figure 2"]
    assert a["tables"] == ["TABLE I", "TABLE II"] and a["equations"] == ["[i]", "[ii]"]
    assert [s["text"] for s in a["sections"]] == ["I.", "II.", "IIA", "IIB", "III."]
    text = docx_text(alt.output).replace("\u00a0", " ")
    for expected in ("I. - Introduction", "Plate A | The first example.", "TABLE II -- Parameters of the second run.",
                     "Eq. [i]", "Section II and the setup in Section IIA", "Tbl. II lists more", "Data Availability"):
        assert expected in text, expected
    assert "Plate A" not in docx_text(generic.output) and "Figure 1" not in text


# --------------------------------------------------------------------- fixture C: local class


def test_local_class_and_style_are_staged_and_their_macros_expand(tmp_path: Path):
    project, result = build(tmp_path, "local_class")
    assert result.ok, [str(f) for f in result.findings if f.severity is not Severity.INFO]
    staged = {p.relative_to(Path(result.workspace) / "staging").as_posix() for p in (Path(result.workspace) / "staging").rglob("*") if p.is_file()}
    assert {"localclass.cls", "localstyle.sty", "main.tex"} <= staged
    text = docx_text(result.output)
    assert "TERM kernel" in text and "REMARK:" in text and "Appendix Notes" in text
    assert result.summary["numbering"]["unnumbered_headings"] == ["Appendix Notes"]


# ------------------------------------------------------------------ physical figure size check


def test_svgs_with_different_internal_scales_get_the_same_physical_width(tmp_path: Path):
    _, result = build(tmp_path, "scale_pair")
    assert result.ok, [str(f) for f in result.findings if f.severity is not Severity.INFO]
    figures = result.validation.metrics["figures"]
    assert [f["id"] for f in figures] == ["big", "small", "viewbox_only"]
    widths = [f["cx_in"] for f in figures]
    assert max(widths) - min(widths) <= 0.01 * max(widths)  # 326.4pt, 100px and viewBox-only SVGs: one width
    for figure in figures:  # aspect of the SVG (4:3) is preserved
        assert figure["aspect"] == pytest.approx(4 / 3, rel=0.01)
    assert widths[0] == pytest.approx(0.98 * 6.5, rel=0.002)  # the generic layout: 0.98 of a 6.5 in single column


def test_widths_pandoc_cannot_read_are_normalised_so_the_image_is_not_left_at_intrinsic_size(tmp_path: Path):
    _, config = setup_project(tmp_path, "scale_pair")
    result = run_build(config)
    staged = (Path(result.workspace) / "staging" / "main.tex").read_text(encoding="utf-8")
    assert "\\columnwidth" not in staged and staged.count("width=0.98\\linewidth") == 3


# ---------------------------------------------------------------- partial table rules


def test_cmidrule_and_cline_ranges_do_not_leak_into_table_cells(tmp_path: Path):
    tables = (
        "\\documentclass{article}\\usepackage{booktabs}\\begin{document}\n"
        "\\section{Tablas}\n"
        "\\begin{table}\\centering\\caption{Reglas parciales}\\label{tab:r}\n"
        "\\begin{tabular}{llll}\\toprule\n& \\multicolumn{3}{c}{Grupo} \\\\\n"
        "\\cmidrule(lr){2-2}\\cmidrule(lr){3-3}\\cmidrule(l){4-4}\n"
        "Nombre & Uno & Dos & Tres \\\\\n\\midrule\nfila & 1 & 2 & 3 \\\\\n\\cline{2-3}\n"
        "otra & 4 & 5 & 6 \\\\\n\\bottomrule\n\\end{tabular}\n\\end{table}\n"
        "Ver el \\ref{tab:r}.\n\\end{document}\n"
    )
    _, result = build(tmp_path, "inline_bib", edit={"main.tex": tables})
    assert result.ok, [str(f) for f in result.findings if f.severity is not Severity.INFO]
    xml = zipfile.ZipFile(result.output).read("word/document.xml").decode("utf-8")
    import re

    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<w:tc>.*?</w:tc>", xml, flags=re.S)]
    assert "Grupo" in cells
    assert "Nombre" in cells and "otra" in cells  # exact cell texts: nothing glued in front of them
    assert not [c for c in cells if re.match(r"\(?[lr]*\)?\d+-\d+", c)], cells


# ---------------------------------------------------------------- inline (manual) bibliography


def test_inline_thebibliography_is_kept_as_numbered_entries_with_working_citations(tmp_path: Path):
    _, result = build(tmp_path, "inline_bib")
    assert result.ok, [str(f) for f in result.findings if f.severity is not Severity.INFO]
    assert result.summary["bibliography_mode"] == "manual"
    text = docx_text(result.output).replace("\u00a0", " ")
    assert "We build on [1] and [2, 1], see also [2, p. 3]." in text
    assert "[1] D. E. Knuth," in text and "[2] L. Lamport," in text and "& 2nd ed." in text
    assert result.validation.metrics["bibliography_paragraphs"] == 2 and "\\cite" not in text


def test_ambiguous_bibliography_sources_fail_the_build_with_a_diagnostic(tmp_path: Path):
    both = (FIXTURES / "inline_bib" / "main.tex").read_text(encoding="utf-8").replace("\\end{document}", "\\bibliography{extra}\n\\end{document}")
    project, config = setup_project(tmp_path, "inline_bib", edit={"main.tex": both})
    (project / "extra.bib").write_text("@article{knuth84,title={T}}", encoding="utf-8")
    result = run_build(config)
    assert not result.ok and error_codes(result) == ["L2W-E-BIB-001"] and result.output is None


def test_explicit_precedence_in_the_configuration_resolves_the_ambiguity(tmp_path: Path):
    both = (FIXTURES / "inline_bib" / "main.tex").read_text(encoding="utf-8").replace("\\end{document}", "\\bibliography{extra}\n\\end{document}")
    project, config = setup_project(tmp_path, "inline_bib", edit={"main.tex": both}, extra={"bibliography": {"prefer": "inline"}})
    (project / "extra.bib").write_text("@article{knuth84,title={T}}", encoding="utf-8")
    assert run_build(config).ok


# -------------------------------------------------------------------------- failure behaviour


MAIN = (FIXTURES / "generic_article" / "main.tex").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("Section~\\ref{sec:method}", "Section~\\ref{sec:nowhere}", "L2W-E-REF-001"),
        ("\\label{fig:two}", "\\label{fig:one}", "L2W-E-LABEL-001"),
        ("\\cite{alpha}", "\\cite{ghost}", "L2W-E-CITE-001"),
        ("\\citep{beta,gamma}", "\\nameref{sec:method}", "L2W-E-REF-006"),
        ("\\citep{beta,gamma}", "\\citeauthor*{beta}\\mycite{beta}", "L2W-E-CITE-002"),
        ("E = mc^2 \\label{eq:energy}", "E \\begin{foo} a & b \\end{foo} \\label{eq:energy}", "L2W-E-MATH-001"),
        ("\\begin{aligned}", "\\begin{aligned}\\intertext{x}", "L2W-E-MATH-003"),
    ],
)
def test_semantic_corruption_fails_the_build_without_writing_output(tmp_path: Path, old: str, new: str, code: str):
    assert old in MAIN
    project, config = setup_project(tmp_path, "generic_article", edit={"main.tex": MAIN.replace(old, new)})
    result = run_build(config)
    assert not result.ok and code in error_codes(result), [str(f) for f in result.findings]
    assert result.output is None and not (tmp_path / "out").exists()


def test_a_failed_build_never_destroys_the_previous_output(tmp_path: Path):
    project, config = setup_project(tmp_path, "generic_article")
    first = run_build(config)
    assert first.ok
    before = first.output.read_bytes()
    (project / "main.tex").write_text(MAIN.replace("\\cite{alpha}", "\\cite{ghost}"), encoding="utf-8")
    second = run_build(config)
    assert not second.ok and first.output.read_bytes() == before
    assert [p.name for p in first.output.parent.iterdir()] == ["main.docx"]  # no temp files left behind


def test_successful_rebuild_replaces_the_output_atomically_and_honours_output_override(tmp_path: Path):
    project, config = setup_project(tmp_path, "generic_article")
    first = run_build(config)
    (project / "main.tex").write_text(MAIN.replace("The data are available on request.", "Changed."), encoding="utf-8")
    second = run_build(config)
    assert second.ok and second.output == first.output and "Changed." in docx_text(second.output)
    custom = tmp_path / "elsewhere" / "named.docx"
    third = run_build(config, output=custom)
    assert third.ok and third.output == custom.resolve() and custom.is_file()


def test_unknown_content_bearing_command_is_an_error_unless_the_user_allows_it(tmp_path: Path):
    edited = MAIN.replace("\\section*{Data Availability}", "\\section*{Data Availability}\n\\mystery{prose that would be lost}")
    _, config = setup_project(tmp_path / "a", "generic_article", edit={"main.tex": edited})
    result = run_build(config)
    assert not result.ok and any(f.code == "L2W-E-TEX-001" and "mystery" in f.message for f in result.findings)
    _, allowed = setup_project(tmp_path / "b", "generic_article", edit={"main.tex": edited}, extra={"tex": {"allow_commands": ["mystery"]}})
    accepted = run_build(allowed)
    assert accepted.ok and any(f.code == "L2W-I-TEX-003" and "mystery" in f.message for f in accepted.findings)


def test_warnings_pass_by_default_and_fail_when_the_configuration_says_so(tmp_path: Path):
    edited = MAIN.replace("width=0.5\\columnwidth]{figures/one.svg}", "width=\\mystery]{figures/one.svg}")
    assert edited != MAIN
    _, config = setup_project(tmp_path / "a", "generic_article", edit={"main.tex": edited})
    result = run_build(config)
    assert result.ok and any(f.code == "L2W-W-FIG-003" for f in result.findings)
    _, strict = setup_project(tmp_path / "b", "generic_article", edit={"main.tex": edited}, extra={"validation": {"fail_on": "warning"}})
    assert not run_build(strict).ok


def test_failed_build_keeps_the_workspace_unless_the_configuration_discards_it(tmp_path: Path):
    broken = MAIN.replace("\\cite{alpha}", "\\cite{ghost}")
    _, config = setup_project(tmp_path / "keep", "generic_article", edit={"main.tex": broken})
    kept = run_build(config)
    assert not kept.ok and kept.manifest_path.is_file()
    assert json.loads(kept.manifest_path.read_text(encoding="utf-8"))["status"] == "failed"
    _, discard = setup_project(tmp_path / "drop", "generic_article", edit={"main.tex": broken}, extra={"workspace": {"root": str(tmp_path / "drop" / "workspace"), "keep_on_failure": False}})
    dropped = run_build(discard)
    assert not dropped.ok and dropped.workspace is None and not (tmp_path / "drop" / "workspace").exists()
    forced = run_build(discard, keep_workspace=True)
    assert forced.workspace is not None and (tmp_path / "drop" / "workspace").is_dir()


def test_missing_pandoc_is_a_build_error(tmp_path: Path):
    _, config = setup_project(tmp_path, "generic_article")
    with pytest.raises(BuildError, match="not found"):
        run_build(config, which=fake_which(set()))


def test_unknown_profile_is_a_configuration_error_before_anything_is_written(tmp_path: Path):
    _, config = setup_project(tmp_path, "generic_article", profile="no-such-profile")
    with pytest.raises(ConfigError, match="unknown profile"):
        run_build(config)
    assert not (tmp_path / "workspace").exists()


# ----------------------------------------------------------------------- citations and CSL


def test_common_citation_commands_are_resolved_by_citeproc_without_raw_syntax(tmp_path: Path):
    _, result = build(tmp_path, "citations")
    assert result.ok, [str(f) for f in result.findings if f.severity is not Severity.INFO]
    text = docx_text(result.output).replace("\u00a0", " ")
    assert "\\cite" not in text and "\\autocite" not in text and "(?)" not in text and "[?]" not in text
    assert "Plain (Alpha 2020), grouped (Alpha 2020; Beta 2021)" in text
    assert "textual Gamma (2022)" in text and "(Alpha 2020, 5)" in text
    assert result.validation.metrics["bibliography_paragraphs"] == 3
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert sorted(manifest["bibliography"]["cited_keys"]) == ["alpha", "beta", "gamma"]


def test_the_csl_style_comes_from_the_configuration(tmp_path: Path):
    csl = FIXTURES / "csl" / "numeric.csl"
    _, result = build(tmp_path, "citations", extra={"bibliography": {"csl": str(csl)}})
    assert result.ok, [str(f) for f in result.findings if f.severity is not Severity.INFO]
    text = docx_text(result.output).replace("\u00a0", " ")
    assert "Plain [1], grouped [1, 2], parenthetical [2]" in text and "[3] Gus Gamma, A third work" in text
    assert json.loads(result.manifest_path.read_text(encoding="utf-8"))["bibliography"]["csl"] == "numeric.csl"


def test_the_csl_style_can_come_from_the_profile(tmp_path: Path):
    profiles = tmp_path / "profiles"
    (profiles / "styles").mkdir(parents=True)
    shutil.copy(FIXTURES / "csl" / "numeric.csl", profiles / "styles" / "numeric.csl")
    (profiles / "numeric-profile.yaml").write_text(
        "profile_schema_version: 1\nid: numeric-profile\nextends: generic\nbibliography:\n  csl: styles/numeric.csl\n", encoding="utf-8"
    )
    _, result = build(tmp_path, "citations", profile="numeric-profile", extra={"profile_dirs": [str(profiles)]})
    assert result.ok and "Plain [1], grouped [1, 2]" in docx_text(result.output)


def test_a_missing_csl_file_fails_before_conversion(tmp_path: Path):
    _, result = build(tmp_path, "citations", extra={"bibliography": {"csl": str(tmp_path / "gone.csl")}})
    assert not result.ok and "L2W-E-BIB-013" in error_codes(result)
