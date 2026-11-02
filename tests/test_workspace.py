from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from latex2word.errors import WorkspaceSafetyError
from latex2word.workspace import (
    APP_NAME,
    MARKER_NAME,
    OwnershipMarker,
    Workspace,
    default_workspace_path,
    is_owned_workspace,
    read_marker,
    write_marker,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def test_default_workspace_path_is_inside_project(project: Path):
    assert default_workspace_path(project) == project / ".latex2word"


def test_create_writes_marker(project: Path, home: Path):
    workspace = Workspace.create(default_workspace_path(project), project, run_id="run-1", home=home)
    marker = read_marker(workspace.path)
    assert (workspace.path / MARKER_NAME).is_file()
    assert marker.application == APP_NAME
    assert marker.schema_version == 1
    assert marker.run_id == "run-1"
    assert Path(marker.workspace_path) == workspace.path
    assert marker.created_at
    raw = (workspace.path / MARKER_NAME).read_bytes()
    assert b"\r\n" not in raw and json.loads(raw.decode("utf-8"))["application"] == APP_NAME


def test_owned_workspace_is_recognised(project: Path, home: Path):
    workspace = Workspace.create(default_workspace_path(project), project, home=home)
    assert is_owned_workspace(workspace.path)
    assert Workspace.open(workspace.path, project).marker == workspace.marker


def test_create_reuses_owned_workspace_without_touching_contents(project: Path, home: Path):
    first = Workspace.create(default_workspace_path(project), project, run_id="a", home=home)
    (first.path / "keep.txt").write_text("x", encoding="utf-8")
    second = Workspace.create(default_workspace_path(project), project, run_id="b", home=home)
    assert second.marker.run_id == "a"
    assert (second.path / "keep.txt").exists()


def test_create_adopts_empty_directory(project: Path, home: Path):
    target = project / "empty"
    target.mkdir()
    assert is_owned_workspace(Workspace.create(target, project, home=home).path)


def test_create_refuses_populated_unowned_directory(project: Path, home: Path):
    target = project / "src"
    target.mkdir()
    (target / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceSafetyError, match="not empty"):
        Workspace.create(target, project, home=home)
    assert (target / "a.txt").exists()
    assert not (target / MARKER_NAME).exists()


def test_directory_without_marker_is_not_owned_and_clean_refuses(project: Path, home: Path):
    target = project / "scratch"
    target.mkdir()
    (target / "data.txt").write_text("precious", encoding="utf-8")
    assert not is_owned_workspace(target)
    with pytest.raises(WorkspaceSafetyError, match="no .latex2word-owner.json"):
        Workspace(target, project).clean(home=home)
    assert (target / "data.txt").read_text(encoding="utf-8") == "precious"


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"schema_version": 1, "application": "other-tool", "workspace_path": "{p}", "created_at": "t"}),
        json.dumps({"schema_version": 2, "application": "latex2word", "workspace_path": "{p}", "created_at": "t"}),
        json.dumps({"schema_version": True, "application": "latex2word", "workspace_path": "{p}", "created_at": "t"}),
        json.dumps({"schema_version": 1, "application": "latex2word", "created_at": "t"}),
        json.dumps({"schema_version": 1, "application": "latex2word", "workspace_path": "{p}", "created_at": "t", "run_id": 5}),
        "\ufffd\ufffd",
    ],
)
def test_tampered_marker_is_rejected(project: Path, home: Path, content: str):
    workspace = Workspace.create(default_workspace_path(project), project, home=home)
    marker_path = workspace.path / MARKER_NAME
    marker_path.write_text(content.replace("{p}", json.dumps(str(workspace.path))[1:-1]), encoding="utf-8")
    (workspace.path / "artifact.txt").write_text("x", encoding="utf-8")
    assert not is_owned_workspace(workspace.path)
    with pytest.raises(WorkspaceSafetyError):
        workspace.clean(home=home)
    assert (workspace.path / "artifact.txt").exists()


def test_marker_pointing_elsewhere_is_rejected(project: Path, home: Path):
    workspace = Workspace.create(default_workspace_path(project), project, home=home)
    copy = project / "copied"
    shutil.copytree(workspace.path, copy)
    assert not is_owned_workspace(copy)
    with pytest.raises(WorkspaceSafetyError, match="different directory"):
        Workspace(copy, project).clean(home=home)
    assert copy.exists() and workspace.path.exists()


def test_refuses_filesystem_root(project: Path, home: Path):
    root = Path(project.anchor)
    with pytest.raises(WorkspaceSafetyError, match="filesystem root"):
        Workspace(root, project).clean(home=home)
    with pytest.raises(WorkspaceSafetyError, match="filesystem root"):
        Workspace.create(root, project, home=home)


def test_refuses_project_root_even_with_valid_marker(project: Path, home: Path):
    write_marker(project, OwnershipMarker.new(project))
    with pytest.raises(WorkspaceSafetyError, match="project root"):
        Workspace(project, project).clean(home=home)
    assert (project / "main.tex").exists()


def test_refuses_home_directory_even_with_valid_marker(project: Path, home: Path):
    write_marker(home, OwnershipMarker.new(home))
    (home / "doc.txt").write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceSafetyError, match="home directory"):
        Workspace(home, project).clean(home=home)
    assert (home / "doc.txt").exists()


def test_real_home_is_detected_via_environment(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    write_marker(fake_home, OwnershipMarker.new(fake_home))
    with pytest.raises(WorkspaceSafetyError, match="home directory"):
        Workspace(fake_home, project).clean()


def test_refuses_parent_of_project_root(project: Path, home: Path):
    parent = project.parent
    write_marker(parent, OwnershipMarker.new(parent))
    with pytest.raises(WorkspaceSafetyError, match="parent of the project root"):
        Workspace(parent, project).clean(home=home)
    assert project.exists()


def test_refuses_parent_of_home(project: Path, home: Path):
    target = home.parent / "outer"
    inner_home = target / "user"
    inner_home.mkdir(parents=True)
    write_marker(target, OwnershipMarker.new(target))
    with pytest.raises(WorkspaceSafetyError, match="parent of the home directory"):
        Workspace(target, project).clean(home=inner_home)


def test_clean_removes_owned_workspace_only(project: Path, home: Path):
    workspace = Workspace.create(default_workspace_path(project), project, home=home)
    nested = workspace.path / "build" / "deep"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("x", encoding="utf-8")

    workspace.clean(home=home)

    assert not workspace.path.exists()
    assert (project / "main.tex").exists() and project.exists()


def test_symlinked_workspace_is_refused_and_target_survives(project: Path, home: Path, tmp_path: Path):
    real = Workspace.create(tmp_path / "real-ws", project, home=home)
    link = project / "link-ws"
    try:
        link.symlink_to(real.path, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Unprivileged Windows cannot create symlinks, but can create junctions.
        made = sys.platform == "win32" and subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(real.path)], capture_output=True, check=False
        ).returncode == 0
        if not made:
            pytest.skip("neither symlinks nor junctions can be created on this machine")
    assert not is_owned_workspace(link)
    with pytest.raises(WorkspaceSafetyError):
        Workspace(link, project).clean(home=home)
    with pytest.raises(WorkspaceSafetyError):
        Workspace.create(link, project, home=home)
    assert real.path.exists()
