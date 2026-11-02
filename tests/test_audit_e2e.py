"""End-to-end audit evidence on ``tests/fixtures/audit/clean_article`` (reports/audit/reproducibility.md).

* determinism: two builds in different workspaces give a byte-identical DOCX;
* package integrity: the independent oracle (tools/audit/ooxml_oracle.py) finds no fault;
* math preservation: OMML structure and content checked against expectations derived by hand from the source.

With an uncertified Pandoc (the audit container has 3.1.3) every build fails on L2W-E-LAYOUT-090 because that
Pandoc's own default styles.xml breaks the schema order; only that one check is disabled then, and the reason is
recorded. With the certified Pandoc the full validator runs.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import shutil
import subprocess
import zipfile
from collections import Counter
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT
from lxml import etree
from pandockit import PANDOC, requires_pandoc

from latex2word.build import run_build
from latex2word.config import load_config
from latex2word.pandoc_compat import CERTIFIED_VERSIONS

pytestmark = requires_pandoc

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "audit" / "clean_article"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_SPEC = importlib.util.spec_from_file_location("ooxml_oracle", REPO_ROOT / "tools" / "audit" / "ooxml_oracle.py")
oracle = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(oracle)


def _pandoc_certified() -> bool:
    out = subprocess.run([PANDOC, "--version"], capture_output=True, text=True).stdout
    match = re.search(r"pandoc\S*\s+(\d+)\.(\d+)", out)
    return bool(match) and (int(match.group(1)), int(match.group(2))) in CERTIFIED_VERSIONS


@pytest.fixture
def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if not _pandoc_certified():
        import latex2word.ooxml.validation as validation

        monkeypatch.setattr(validation, "_check_schema_order", lambda roots, result: None)
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)

    def run(n: int) -> Path:
        data = {
            "schema_version": 1,
            "project": {"name": "clean", "root": str(project), "main": "main.tex"},
            "profile": "generic",
            "workspace": {"root": str(tmp_path / f"ws{n}")},
            "output": {"directory": str(tmp_path / f"out{n}")},
        }
        config = tmp_path / f"c{n}.yaml"
        config.write_text(yaml.safe_dump(data), encoding="utf-8")
        result = run_build(load_config(config))
        assert result.ok, [str(f) for f in result.errors]
        return result.output

    return run


def test_build_is_byte_reproducible_and_passes_the_independent_oracle(build):
    first, second = build(1), build(2)
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(second.read_bytes()).hexdigest()
    result = oracle.check(first)
    assert result["ok"], result["errors"]
    assert result["facts"]["field_instructions"].get("TOC") == 1
    assert result["facts"]["omml_tex_remnants"] == [] and result["facts"]["text_tex_remnants"] == []


def test_math_structure_matches_the_source(build):
    root = etree.fromstring(zipfile.ZipFile(build(1)).read("word/document.xml"))
    kinds = Counter(etree.QName(e).localname for e in root.iter() if isinstance(e.tag, str) and e.tag.startswith(f"{{{M}}}"))
    # \frac{a}{b}, \binom{N}{n}, \frac{\sin x}{x}
    assert kinds["f"] == 3
    # \sqrt[3]{...} keeps its degree
    assert kinds["rad"] == 1 and ["".join(d.itertext()) for d in root.iter(f"{{{M}}}deg")] == ["3"]
    chars = [n.find(f"{{{M}}}naryPr/{{{M}}}chr") for n in root.iter(f"{{{M}}}nary")]
    assert [c.get(f"{{{M}}}val") if c is not None else "∫" for c in chars] == ["∫", "∑"]
    assert kinds["eqArr"] == 0  # alignment rows are matrices: no literal '&'
    text = "".join(t.text or "" for t in root.iter(f"{{{M}}}t"))
    assert "&" not in text and "2pt" not in text
    # F-01: the row "[x,y] &= z" keeps its bracketed content
    delimiters = [
        (d.find(f"{{{M}}}dPr/{{{M}}}begChr"), "".join(t.text or "" for t in d.iter(f"{{{M}}}t")))
        for d in root.iter(f"{{{M}}}d")
    ]
    assert any(b is not None and b.get(f"{{{M}}}val") == "[" and inner == "x,y" for b, inner in delimiters)


def test_text_escapes_and_accents_survive(build):
    xml = zipfile.ZipFile(build(1)).read("word/document.xml").decode("utf-8")
    text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))
    for expected in ("100%", "&amp;", "$5", "#", "_", "{", "}", "Pérez", "Müller", "Ñandú", "Łukasz"):
        assert expected in text, expected
