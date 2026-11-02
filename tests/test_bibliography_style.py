"""F-11: a declared bibliography style is never silently replaced by a different CSL (reports/audit/remediation-phase2.md)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import REPO_ROOT
from projectkit import make_project

from latex2word.bibliography import csl_identity, declared_styles
from latex2word.bibliography import style_mismatch_findings as _mismatch
from latex2word.profiles import load_profile
from latex2word.source_graph import build_source_graph

# the style table is profile data (generic.yaml, inherited by every built-in profile)
TABLE = load_profile("generic", ()).bibliography["style_csl"]


def style_mismatch_findings(graph, csl):
    return _mismatch(graph, csl, TABLE)


NUMERIC_CSL = REPO_ROOT / "tests" / "fixtures" / "semantic" / "csl" / "numeric.csl"


def _csl(path: Path, style_id: str, short: str | None = None) -> Path:
    extra = f"<title-short>{short}</title-short>" if short else ""
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><style xmlns="http://purl.org/net/xbiblio/csl" version="1.0" class="in-text">'
        f"<info><title>T</title>{extra}<id>http://www.zotero.org/styles/{style_id}</id></info></style>", encoding="utf-8")
    return path


def graph(tmp_path: Path, preamble: str):
    root = make_project(tmp_path, {"main.tex": f"\\documentclass{{article}}\n{preamble}\n\\begin{{document}}x\\end{{document}}\n"})
    return build_source_graph(root, root / "main.tex")


def codes(findings):
    return [f.code for f in findings]


def test_biblatex_style_apa_without_csl_warns(tmp_path: Path):
    findings = style_mismatch_findings(graph(tmp_path, "\\usepackage[backend=biber,style=apa]{biblatex}"), None)
    assert codes(findings) == ["L2W-W-BIB-014"]
    assert "'apa'" in findings[0].message and "chicago-author-date" in findings[0].message
    assert findings[0].location.line == 2


def test_explicit_matching_csl_is_accepted(tmp_path: Path):
    csl = _csl(tmp_path / "my-style.csl", "apa")
    assert codes(style_mismatch_findings(graph(tmp_path, "\\usepackage[style=apa]{biblatex}"), csl)) == ["L2W-I-BIB-015"]


def test_explicit_csl_of_another_style_warns(tmp_path: Path):
    findings = style_mismatch_findings(graph(tmp_path, "\\usepackage[style=apa]{biblatex}"), NUMERIC_CSL)
    assert codes(findings) == ["L2W-W-BIB-015"]


def test_bibtex_style_maps_through_the_alias_table(tmp_path: Path):
    csl = _csl(tmp_path / "x.csl", "ieee")
    assert codes(style_mismatch_findings(graph(tmp_path, "\\bibliographystyle{IEEEtran}"), csl)) == ["L2W-I-BIB-015"]


@pytest.mark.parametrize("with_csl", [False, True])
def test_unknown_style(tmp_path: Path, with_csl: bool):
    findings = style_mismatch_findings(graph(tmp_path, "\\usepackage[style=myhouse]{biblatex}"), NUMERIC_CSL if with_csl else None)
    assert codes(findings) == (["L2W-W-BIB-016"] if with_csl else ["L2W-W-BIB-014"])


def test_no_declared_style_gives_no_style_finding(tmp_path: Path):
    assert style_mismatch_findings(graph(tmp_path, "\\usepackage{biblatex}"), None) == []


def test_default_csl_equivalent_is_not_a_mismatch(tmp_path: Path):
    assert style_mismatch_findings(graph(tmp_path, "\\usepackage[style=chicago-authordate]{biblatex}"), None) == []


def test_bibstyle_option_and_commented_declarations(tmp_path: Path):
    g = graph(tmp_path, "\\usepackage[bibstyle=apa,citestyle=apa]{biblatex}\n% \\bibliographystyle{plain}")
    assert [(d.name, d.origin) for d in declared_styles(g)] == [("apa", "biblatex bibstyle=")]


def test_csl_identity_reads_id_title_short_and_stem(tmp_path: Path):
    assert csl_identity(_csl(tmp_path / "local-name.csl", "apa", "APA")) == {"local-name", "apa"}


def test_without_a_profile_table_every_declared_style_is_unverifiable(tmp_path: Path):
    g = graph(tmp_path, "\\usepackage[style=apa]{biblatex}")
    assert codes(_mismatch(g, NUMERIC_CSL, None)) == ["L2W-W-BIB-016"]
    assert codes(_mismatch(g, None, None)) == ["L2W-W-BIB-014"]


def test_ieee_conference_profile_inherits_the_table():
    assert load_profile("ieee-conference", ()).bibliography["style_csl"]["apa"] == ["apa"]
