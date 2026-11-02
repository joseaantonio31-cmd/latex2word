"""The DOCX carries no path of the build machine: Pandoc's ``csl`` custom property is removed at finalisation."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT
from lxml import etree
from pandockit import requires_pandoc

import latex2word.ooxml.finalize as finalize_module
from latex2word.build import run_build
from latex2word.config import load_config
from latex2word.ooxml.custom_properties import CUSTOM_PART, remove_build_path_properties
from latex2word.ooxml.package import OpcPackage

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "semantic"
MARKER = "L2WPRIVACYMARKER5150"
CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"


def _custom_xml(properties: list[tuple[str, str]]) -> bytes:
    body = "".join(
        f'<property fmtid="{FMTID}" pid="{pid}" name="{name}"><vt:lpwstr>{value}</vt:lpwstr></property>'
        for pid, (name, value) in enumerate(properties, start=2)
    )
    return (f'<?xml version="1.0" encoding="UTF-8"?><Properties xmlns="{CUSTOM_NS}" '
            f'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">{body}</Properties>').encode()


def _properties(xml: bytes) -> list[tuple[str, str, str]]:
    root = etree.fromstring(xml)
    assert root.tag == f"{{{CUSTOM_NS}}}Properties"
    return [(p.get("name"), p.get("pid"), "".join(p.itertext())) for p in root]


# ---------------------------------------------------------------- unit: only the build-path property goes


def test_only_the_csl_property_is_removed_and_the_others_keep_name_pid_and_value():
    package = OpcPackage({CUSTOM_PART: _custom_xml([
        ("bibliography", ""), ("csl", "C:\\Users\\someone\\styles\\apa.csl"), ("reference-section-title", "References"),
        ("project-note", "C:\\a\\user\\value.txt"),  # a user property that merely looks like a path stays
    ])})
    assert remove_build_path_properties(package) == ["csl"]
    assert _properties(package.read(CUSTOM_PART)) == [
        ("bibliography", "2", ""), ("reference-section-title", "4", "References"), ("project-note", "5", "C:\\a\\user\\value.txt"),
    ]


def test_a_package_without_custom_properties_or_without_csl_is_unchanged():
    assert remove_build_path_properties(OpcPackage({"word/document.xml": b"<x/>"})) == []
    original = _custom_xml([("reference-section-title", "References")])
    package = OpcPackage({CUSTOM_PART: original})
    assert remove_build_path_properties(package) == []
    assert _properties(package.read(CUSTOM_PART)) == _properties(original)


# ---------------------------------------------------------------- end to end


def _build(tmp_path: Path, csl: Path | None):
    project = tmp_path / "project"
    if not project.exists():
        shutil.copytree(FIXTURES / "generic_article", project)
    data = {"schema_version": 1, "project": {"name": "privacy", "root": str(project), "main": "main.tex"},
            "profile": "generic", "workspace": {"root": str(tmp_path / "workspace")},
            "output": {"directory": str(tmp_path / "out")}, "tools": {"timeout_s": 60}}
    if csl is not None:
        data["bibliography"] = {"csl": str(csl)}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    result = run_build(load_config(tmp_path / "config.yaml"))
    assert result.ok, [str(f) for f in result.errors]
    with zipfile.ZipFile(result.output) as package:
        return {name: package.read(name) for name in package.namelist()}, result


def _marked_csl(tmp_path: Path) -> Path:
    csl = tmp_path / MARKER / "styles" / "numeric.csl"
    csl.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "csl" / "numeric.csl", csl)
    return csl


@requires_pandoc
def test_the_build_machine_path_of_the_csl_is_absent_from_every_part(tmp_path: Path):
    parts, result = _build(tmp_path, _marked_csl(tmp_path))
    leaks = [name for name, data in parts.items()
             if MARKER.encode() in data or MARKER.encode("utf-16-le") in data or str(tmp_path).encode() in data]
    assert leaks == []
    names = [name for name, _pid, _value in _properties(parts[CUSTOM_PART])]
    assert "csl" not in names and "reference-section-title" in names  # unrelated properties survive
    assert result.final is not None


@requires_pandoc
def test_removing_the_property_changes_nothing_else_in_the_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    csl = _marked_csl(tmp_path)
    monkeypatch.setattr(finalize_module, "remove_build_path_properties", lambda package: [])
    before, _ = _build(tmp_path, csl)
    monkeypatch.undo()
    after, result = _build(tmp_path, csl)
    assert sorted(before) == sorted(after)
    assert [name for name in after if before[name] != after[name]] == [CUSTOM_PART]
    # the same properties in the same order, minus the csl property
    assert [p for p in _properties(before[CUSTOM_PART]) if p[0] != "csl"] == _properties(after[CUSTOM_PART])
    assert any(p[0] == "csl" and MARKER in p[2] for p in _properties(before[CUSTOM_PART]))
    # the bibliography that the CSL produced is in the document, unchanged
    document = after["word/document.xml"].decode("utf-8")
    assert all(author in document for author in ("Alpha", "Beta", "Gamma"))
    assert result.final is not None


@requires_pandoc
def test_a_build_without_a_configured_csl_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(finalize_module, "remove_build_path_properties", lambda package: [])
    before, _ = _build(tmp_path, None)
    monkeypatch.undo()
    after, _ = _build(tmp_path, None)
    assert before == after
    assert "csl" not in [name for name, _pid, _value in _properties(after[CUSTOM_PART])]
