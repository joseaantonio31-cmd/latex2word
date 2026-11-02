from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT

from latex2word.config import check_project_files, load_config
from latex2word.errors import ConfigError

VALID: dict[str, Any] = {
    "schema_version": 1,
    "project": {"name": "demo", "main": "main.tex"},
    "profile": "ieee-conference",
}


def write_config(directory: Path, data: Any = None, *, name: str = "cfg.yaml", text: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text if text is not None else yaml.safe_dump(data if data is not None else VALID), encoding="utf-8")
    return path


def mutated(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(VALID)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def test_shipped_example_is_valid():
    config = load_config(REPO_ROOT / "examples" / "minimal-project.yaml")
    assert config.profile == "ieee-conference"
    assert config.project.name == "example"


def test_minimal_config_gets_defaults(tmp_path: Path):
    config = load_config(write_config(tmp_path))
    assert config.tools.timeout_s == 120
    assert config.workspace.keep_on_failure is True
    assert config.workspace.root == tmp_path.resolve() / ".latex2word"
    assert config.output.directory == tmp_path.resolve() / "output"
    assert config.validation.fail_on == "error"


@pytest.mark.parametrize(
    ("data", "path"),
    [
        (mutated(schema_version=None), ""),
        (mutated(project=None), ""),
        (mutated(profile=None), ""),
        (mutated(project={"name": "demo"}), "project"),
        (mutated(project={"main": "main.tex"}), "project"),
    ],
)
def test_missing_required_field_fails(tmp_path: Path, data: dict[str, Any], path: str):
    with pytest.raises(ConfigError) as info:
        load_config(write_config(tmp_path, data))
    assert any("required property" in issue.message and issue.path == path for issue in info.value.issues)


@pytest.mark.parametrize("version", [2, 0, "1", 1.5, True, None])
def test_unknown_schema_version_fails(tmp_path: Path, version: Any):
    with pytest.raises(ConfigError) as info:
        load_config(write_config(tmp_path, mutated(schema_version=version) | {"schema_version": version}))
    assert [i.path for i in info.value.issues] == ["schema_version"]
    assert "unsupported schema_version" in info.value.issues[0].message


@pytest.mark.parametrize(
    "data",
    [
        {**VALID, "surprise": 1},
        {**VALID, "project": {"name": "demo", "main": "main.tex", "typo": 1}},
        {**VALID, "tools": {"timeout_s": 0}},
        {**VALID, "tools": {"timeout_s": "120"}},
        {**VALID, "workspace": {"keep_on_failure": "yes"}},
        {**VALID, "validation": {"fail_on": "info"}},
        {**VALID, "profile": "IEEE Conference"},
        {**VALID, "project": {"name": 123, "main": "main.tex"}},
    ],
)
def test_schema_violations_fail(tmp_path: Path, data: dict[str, Any]):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data))


def test_paths_resolve_against_config_file_not_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = tmp_path / "a" / "proj"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    data = mutated(project={"name": "demo", "root": "proj", "main": "src/main.tex"})
    data["output"] = {"directory": "out/docx"}
    config_path = write_config(tmp_path / "a", data)
    monkeypatch.chdir(elsewhere)

    config = load_config(config_path)

    assert config.project.root == project.resolve()
    assert config.project.main == (project / "src" / "main.tex").resolve()
    assert config.workspace.root == (project / ".latex2word").resolve()
    assert config.output.directory == (project / "out" / "docx").resolve()
    assert config.config_file == config_path.resolve()


def test_relative_config_path_gives_same_result_as_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = write_config(tmp_path / "x")
    monkeypatch.chdir(tmp_path)
    assert load_config(Path("x") / "cfg.yaml") == load_config(path)


def test_to_dict_is_deterministic_and_has_no_run_specific_values(tmp_path: Path):
    path = write_config(tmp_path)
    first, second = load_config(path).to_dict(), load_config(path).to_dict()
    assert first == second
    assert first["project"]["root"] == tmp_path.resolve().as_posix()
    assert set(first) == {
        "config_file", "schema_version", "project", "profile", "tools", "workspace", "output", "validation"
    }


@pytest.mark.parametrize("main", ["../other/main.tex", "sub/../../main.tex"])
def test_main_escaping_project_root_is_rejected(tmp_path: Path, main: str):
    with pytest.raises(ConfigError) as info:
        load_config(write_config(tmp_path, mutated(project={"name": "demo", "main": main})))
    assert info.value.issues[0].path == "project.main"


def test_absolute_main_is_rejected(tmp_path: Path):
    absolute = str((tmp_path / "main.tex").resolve())
    with pytest.raises(ConfigError) as info:
        load_config(write_config(tmp_path, mutated(project={"name": "demo", "main": absolute})))
    assert info.value.issues[0].path == "project.main"


@pytest.mark.parametrize(("section", "key"), [("workspace", "root"), ("output", "directory")])
@pytest.mark.parametrize("value", [".", ".."])
def test_workspace_and_output_must_not_cover_project_root(tmp_path: Path, section: str, key: str, value: str):
    data = mutated(project={"name": "demo", "root": "proj", "main": "main.tex"})
    data[section] = {key: value}
    with pytest.raises(ConfigError) as info:
        load_config(write_config(tmp_path, data))
    assert info.value.issues[0].path == f"{section}.{key}"


def test_missing_main_file_is_not_an_error_for_parsing(tmp_path: Path):
    config = load_config(write_config(tmp_path))
    assert not config.project.main.exists()


def test_check_project_files_requires_existing_main(tmp_path: Path):
    config = load_config(write_config(tmp_path))
    with pytest.raises(ConfigError) as info:
        check_project_files(config)
    assert info.value.issues[0].path == "project.main"
    (tmp_path / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")
    check_project_files(config)


def test_check_project_files_requires_existing_root(tmp_path: Path):
    config = load_config(write_config(tmp_path, mutated(project={"name": "d", "root": "nope", "main": "m.tex"})))
    with pytest.raises(ConfigError) as info:
        check_project_files(config)
    assert info.value.issues[0].path == "project.root"


def test_loader_is_safe_and_never_constructs_python_objects(tmp_path: Path):
    marker = tmp_path / "pwned"
    payload = f"!!python/object/apply:os.mkdir [{str(marker)!r}]"
    path = write_config(tmp_path, text=f"schema_version: 1\nproject: {payload}\nprofile: p\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)
    assert not marker.exists()


def test_duplicate_keys_are_rejected_not_silently_overridden(tmp_path: Path):
    text = "schema_version: 1\nproject: {name: a, main: m.tex}\nprofile: p\nprofile: q\n"
    with pytest.raises(ConfigError, match="duplicate key 'profile'"):
        load_config(write_config(tmp_path, text=text))


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string\n", "a: [unclosed\n"])
def test_non_mapping_or_malformed_yaml_fails_cleanly(tmp_path: Path, text: str):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text=text))


def test_unreadable_file_is_a_config_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml")


# ---------------------------------------------------------------- M3: profile dirs, bibliography, output name


def test_m3_keys_are_resolved_relative_to_the_right_base(tmp_path: Path):
    data = mutated(
        project={"name": "demo", "root": "proj", "main": "main.tex"},
        profile_dirs=["profiles", "../shared/profiles"],
        bibliography={"mode": "citeproc", "prefer": "external", "files": ["refs/a.bib", "b.bib"], "csl": "styles/x.csl"},
        output={"directory": "out", "filename": "paper.docx"},
    )
    config = load_config(write_config(tmp_path / "cfgdir", data))
    base = (tmp_path / "cfgdir").resolve()
    assert config.profile_dirs == (base / "profiles", (base / "../shared/profiles").resolve())
    assert config.bibliography.files == ("refs/a.bib", "b.bib") and config.bibliography.csl == base / "styles" / "x.csl"
    assert (config.bibliography.mode, config.bibliography.prefer) == ("citeproc", "external")
    assert config.output.filename == "paper.docx"
    shown = config.to_dict()
    assert shown["bibliography"]["csl"].endswith("styles/x.csl") and shown["profile_dirs"][0].endswith("profiles")
    assert shown["output"]["filename"] == "paper.docx"


def test_m3_keys_are_absent_from_the_normalised_form_when_unset(tmp_path: Path):
    shown = load_config(write_config(tmp_path)).to_dict()
    assert "bibliography" not in shown and "profile_dirs" not in shown and "filename" not in shown["output"]


@pytest.mark.parametrize(
    ("change", "path"),
    [
        ({"bibliography": {"mode": "magic"}}, "bibliography.mode"),
        ({"bibliography": {"prefer": "both"}}, "bibliography.prefer"),
        ({"bibliography": {"files": []}}, "bibliography.files"),
        ({"bibliography": {"files": ["/abs/refs.bib"]}}, "bibliography.files.0"),
        ({"bibliography": {"files": ["../outside.bib"]}}, "bibliography.files.0"),
        ({"bibliography": {"unknown": 1}}, "bibliography"),
        ({"output": {"filename": "paper.txt"}}, "output.filename"),
        ({"output": {"filename": "sub/paper.docx"}}, "output.filename"),
        ({"profile_dirs": "profiles"}, "profile_dirs"),
    ],
)
def test_invalid_m3_keys_are_rejected_with_their_path(tmp_path: Path, change: dict, path: str):
    with pytest.raises(ConfigError) as info:
        load_config(write_config(tmp_path, mutated(**change)))
    assert any(issue.path == path or issue.path.startswith(path) for issue in info.value.issues), info.value.issues


def test_tex_allow_lists_are_loaded_and_round_trip(tmp_path: Path):
    config = load_config(write_config(tmp_path, mutated(tex={"allow_commands": ["mystery"], "allow_environments": ["oddbox"]})))
    assert config.tex.allow_commands == ("mystery",) and config.tex.allow_environments == ("oddbox",)
    assert config.to_dict()["tex"] == {"allow_commands": ["mystery"], "allow_environments": ["oddbox"]}
    assert "tex" not in load_config(write_config(tmp_path / "plain")).to_dict()


@pytest.mark.parametrize("tex", [{"allow_commands": ["not valid"]}, {"allow_commands": ["a", "a"]}, {"allow": ["x"]}, {"allow_environments": [3]}])
def test_invalid_tex_allow_lists_are_rejected(tmp_path: Path, tex: dict):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, mutated(tex=tex)))
