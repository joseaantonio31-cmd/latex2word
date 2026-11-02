from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from conftest import PRIVATE_MARKERS, REPO_ROOT

from latex2word.errors import ConfigError, ProfileError
from latex2word.profiles import available_profiles, load_profile

FIXTURE_PROFILES = REPO_ROOT / "tests" / "fixtures" / "semantic" / "profiles"


def write_profile(directory: Path, name: str, data: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump({"profile_schema_version": 1, "id": name, **data}, sort_keys=False), encoding="utf-8")
    return path


def test_generic_profile_defines_semantics_without_publisher_layout():
    profile = load_profile("generic")
    numbering = profile.numbering
    assert profile.chain == ("generic",)
    assert numbering["figures"]["format"] == "Figure {n}" and numbering["figures"]["reference"] == "Figure {n}"
    assert numbering["tables"]["format"] == "Table {n}"
    assert numbering["equations"]["format"] == "({n})" and numbering["equations"]["reference"] == "({n})"
    assert numbering["sections"]["level1"]["numeral"] == "arabic"
    assert all(numbering[kind]["reset"] == "none" for kind in ("figures", "tables", "equations"))
    assert profile.bibliography["mode"] == "auto"
    assert profile.data["pandoc"]["extra_args"] == []


def test_ieee_profile_overrides_only_what_differs_from_generic():
    generic, ieee = load_profile("generic"), load_profile("ieee-conference")
    assert ieee.chain == ("generic", "ieee-conference")
    n = ieee.numbering
    assert n["sections"]["level1"]["numeral"] == "Roman" and n["sections"]["level2"]["numeral"] == "Alpha"
    assert n["figures"]["format"] == "Fig. {n}." and n["figures"]["reference"] == "Fig. {n}"
    assert n["tables"]["format"] == "TABLE {n}" and n["tables"]["numeral"] == "Roman" and n["tables"]["reference"] == "Table {n}"
    assert n["equations"]["format"] == "({n})"
    # inherited, not re-declared
    assert n["figures"]["numeral"] == generic.numbering["figures"]["numeral"]
    assert ieee.bibliography == generic.bibliography and ieee.required_tools == generic.required_tools


def test_profile_loading_does_not_depend_on_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    expected = load_profile("ieee-conference").to_dict()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "generic.yaml").write_text("this is not a profile", encoding="utf-8")
    (tmp_path / "profiles").mkdir()
    assert load_profile("ieee-conference").to_dict() == expected


def test_custom_profile_directory_is_searched_before_the_built_in_profiles():
    profile = load_profile("alt-numbering", [FIXTURE_PROFILES])
    assert profile.chain == ("generic", "alt-numbering")
    assert profile.numbering["figures"]["format"] == "Plate {n}"
    assert "alt-numbering" in available_profiles([FIXTURE_PROFILES]) and "generic" in available_profiles([FIXTURE_PROFILES])


def test_custom_profile_can_shadow_a_builtin_profile(tmp_path: Path):
    base = yaml.safe_load((REPO_ROOT / "src/latex2word/profiles/generic.yaml").read_text(encoding="utf-8"))
    base["numbering"]["figures"]["format"] = "Shadow {n}"
    write_profile(tmp_path, "generic", {k: v for k, v in base.items() if k not in ("profile_schema_version", "id")})
    assert load_profile("generic", [tmp_path]).numbering["figures"]["format"] == "Shadow {n}"
    assert load_profile("generic").numbering["figures"]["format"] == "Figure {n}"


def test_unknown_profile_lists_the_available_ones():
    with pytest.raises(ProfileError, match=r"unknown profile 'nope'.*generic.*ieee-conference"):
        load_profile("nope")


def test_unknown_keys_fail_clearly_at_any_depth(tmp_path: Path):
    write_profile(tmp_path, "typo", {"extends": "generic", "numbering": {"figures": {"frmat": "x {n}"}}})
    with pytest.raises(ProfileError) as info:
        load_profile("typo", [tmp_path])
    assert any("frmat" in str(issue) and issue.path == "numbering.figures" for issue in info.value.issues)
    write_profile(tmp_path, "top", {"extends": "generic", "publisher_layout": {}})
    with pytest.raises(ProfileError, match="problem"):
        load_profile("top", [tmp_path])


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        ({"numbering": {"figures": {"format": "Fig. {m}"}}}, "unknown field {m}"),
        ({"numbering": {"figures": {"format": "Fig."}}}, "counter field"),
        ({"numbering": {"sections": {"level1": {"format": "{n1}"}}}}, "unknown field {n1}"),
        ({"numbering": {"figures": {"numeral": "greek"}}}, "greek"),
        ({"numbering": {"figures": {"reset": "chapter"}}}, "chapter"),
        ({"bibliography": {"mode": "magic"}}, "magic"),
        ({"bibliography": {"manual": {"citation_format": "[]"}}}, "citation_format"),
        ({"pandoc": {"extra_args": ["--lua-filter=evil.lua"]}}, "managed by latex2word"),
        ({"pandoc": {"extra_args": ["not-an-option"]}}, "does not match"),
        ({"numbering": {"sections": {"level3": {"numeral": "arabic", "format": "{n}", "ref": "{n}", "reference": "{n}"}}}}, None),
    ],
)
def test_malformed_or_inconsistent_profiles_are_rejected(tmp_path: Path, data: dict, needle: str | None):
    if needle is None:  # a consistent override must load
        write_profile(tmp_path, "p", {"extends": "generic", **data})
        assert load_profile("p", [tmp_path]).id == "p"
        return
    write_profile(tmp_path, "p", {"extends": "generic", **data})
    with pytest.raises(ProfileError) as info:
        load_profile("p", [tmp_path])
    assert needle in " ".join(str(issue) for issue in info.value.issues)


def test_profile_without_extends_must_be_complete(tmp_path: Path):
    write_profile(tmp_path, "thin", {"numbering": {"figures": {"format": "F {n}"}}})
    with pytest.raises(ProfileError) as info:
        load_profile("thin", [tmp_path])
    assert any("missing from the resolved profile" in str(i) for i in info.value.issues)


def test_extends_cycles_and_unknown_parents_are_reported(tmp_path: Path):
    write_profile(tmp_path, "a", {"extends": "b"})
    write_profile(tmp_path, "b", {"extends": "a"})
    with pytest.raises(ProfileError, match="cycle"):
        load_profile("a", [tmp_path])
    write_profile(tmp_path, "orphan", {"extends": "ghost"})
    with pytest.raises(ProfileError, match="extends unknown profile 'ghost'"):
        load_profile("orphan", [tmp_path])


def test_profile_file_name_must_match_its_id_and_yaml_must_be_safe(tmp_path: Path):
    (tmp_path / "named.yaml").write_text("profile_schema_version: 1\nid: other\nextends: generic\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="does not match the file name"):
        load_profile("named", [tmp_path])
    (tmp_path / "bad.yaml").write_text("profile_schema_version: 1\nid: bad\nid: bad\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate key"):
        load_profile("bad", [tmp_path])
    (tmp_path / "future.yaml").write_text("profile_schema_version: 2\nid: future\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="unsupported profile_schema_version"):
        load_profile("future", [tmp_path])


def test_profile_csl_is_resolved_relative_to_the_profile_file(tmp_path: Path):
    (tmp_path / "styles").mkdir()
    (tmp_path / "styles" / "mine.csl").write_text("<style/>", encoding="utf-8")
    write_profile(tmp_path, "p", {"extends": "generic", "bibliography": {"csl": "styles/mine.csl"}})
    assert load_profile("p", [tmp_path]).csl == (tmp_path / "styles" / "mine.csl").resolve()
    write_profile(tmp_path, "q", {"extends": "generic", "bibliography": {"csl": "styles/gone.csl"}})
    with pytest.raises(ProfileError, match="does not exist"):
        load_profile("q", [tmp_path])


def test_engine_modules_contain_no_publisher_or_document_class_branches():
    """Publisher behaviour belongs in profiles: no generic module may name a class, publisher or paper."""
    forbidden = re.compile(
        "|".join([r"IEEEtran|elsarticle|acmart|svjour|mdpi", *(re.escape(m) for m in PRIVATE_MARKERS)]), re.I
    )
    offenders = []
    for path in sorted((REPO_ROOT / "src" / "latex2word").rglob("*")):
        if path.suffix in (".py", ".lua", ".json") and "profiles" not in path.parts:
            if forbidden.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)
    assert offenders == []
    branches = re.compile(r"document_?class\s*(==|in\b|\.startswith)|\.profile\s*==|profile_id\s*==|config\.profile\s*==")
    assert [p.name for p in (REPO_ROOT / "src" / "latex2word").rglob("*.py") if branches.search(p.read_text(encoding="utf-8"))] == []
