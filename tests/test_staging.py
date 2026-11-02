from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from projectkit import PDF_BYTES, make_dir_link, make_project, tree_digest

from latex2word.errors import StagingError, WorkspaceSafetyError
from latex2word.source_graph import build_source_graph
from latex2word.staging import (
    StagingManifest,
    staging_dir,
    stage_files,
    verify_staging,
    write_staging_manifest,
)
from latex2word.workspace import Workspace

FILES = {
    "main.tex": "\\input{sections/a}\\input{figures/w}\\bibliography{refs}",
    "sections/a.tex": "a",
    "sections/unused.tex": "never referenced",
    "figures/w.tex": "\\includegraphics{figures/x.pdf}",
    "figures/x.pdf": PDF_BYTES,
    "figures/unused.pdf": PDF_BYTES + b"1",
    "scripts/make.py": "print()",
    "refs.bib": "@a{k,}",
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path, FILES)


@pytest.fixture
def workspace(tmp_path: Path, project: Path) -> Workspace:
    return Workspace.create(tmp_path / "ws", project)


def test_only_reachable_files_are_staged_with_relative_paths(project: Path, workspace: Workspace):
    graph = build_source_graph(project, project / "main.tex")
    manifest = stage_files(project, graph.stageable_files(), workspace)
    staged = sorted(p.relative_to(staging_dir(workspace)).as_posix() for p in staging_dir(workspace).rglob("*") if p.is_file())
    assert staged == sorted(
        ["main.tex", "sections/a.tex", "figures/w.tex", "figures/x.pdf", "refs.bib"]
    )
    assert {f.source: f.dependency_type for f in manifest.files} == {
        "main.tex": "main",
        "sections/a.tex": "tex",
        "figures/w.tex": "tex",
        "figures/x.pdf": "graphic",
        "refs.bib": "bibliography",
    }
    assert all(f.source == f.staged for f in manifest.files)


def test_staged_binary_is_byte_identical_and_hash_recorded(project: Path, workspace: Workspace):
    manifest = stage_files(project, [("figures/x.pdf", "graphic")], workspace)
    copy = (staging_dir(workspace) / "figures" / "x.pdf").read_bytes()
    assert copy == PDF_BYTES
    assert manifest.files[0].sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
    assert manifest.files[0].size == len(PDF_BYTES)


def test_source_tree_is_unchanged_by_staging(project: Path, workspace: Workspace):
    before = tree_digest(project)
    graph = build_source_graph(project, project / "main.tex")
    stage_files(project, graph.stageable_files(), workspace)
    assert tree_digest(project) == before


def test_extra_files_are_staged_when_requested(project: Path, workspace: Workspace):
    manifest = stage_files(project, [("main.tex", "main"), ("scripts/make.py", "extra")], workspace)
    assert manifest.by_source("scripts/make.py").dependency_type == "extra"


def test_restaging_replaces_the_previous_staging_directory(project: Path, workspace: Workspace):
    stage_files(project, [("main.tex", "main"), ("refs.bib", "bibliography")], workspace)
    stage_files(project, [("main.tex", "main")], workspace)
    assert [p.name for p in staging_dir(workspace).rglob("*") if p.is_file()] == ["main.tex"]


@pytest.mark.parametrize("bad", ["../outside.tex", "sections/../../outside.tex", "/abs.tex", "C:/abs.tex"])
def test_paths_escaping_the_project_are_refused(tmp_path: Path, project: Path, workspace: Workspace, bad: str):
    (tmp_path / "outside.tex").write_text("x", encoding="utf-8")
    with pytest.raises(StagingError):
        stage_files(project, [(bad, "tex")], workspace)
    assert not (staging_dir(workspace).parent / "outside.tex").exists()


def test_missing_source_is_refused(project: Path, workspace: Workspace):
    with pytest.raises(StagingError, match="does not exist"):
        stage_files(project, [("nope.tex", "tex")], workspace)


def test_link_leaving_the_project_is_never_followed(tmp_path: Path, project: Path, workspace: Workspace):
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.tex").write_text("secret", encoding="utf-8")
    if not make_dir_link(project / "linked", external):
        pytest.skip("cannot create a directory link on this machine")
    with pytest.raises(StagingError, match="leaves the project root"):
        stage_files(project, [("linked/secret.tex", "tex")], workspace)
    assert not list(staging_dir(workspace).rglob("secret.tex"))


def test_workspace_ownership_is_required(tmp_path: Path, project: Path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("precious", encoding="utf-8")
    fake = Workspace(foreign, project)
    with pytest.raises(WorkspaceSafetyError):
        stage_files(project, [("main.tex", "main")], fake)
    assert sorted(p.name for p in foreign.iterdir()) == ["keep.txt"]


def test_verification_detects_a_tampered_staged_copy(project: Path, workspace: Workspace):
    manifest = stage_files(project, [("refs.bib", "bibliography")], workspace)
    (staging_dir(workspace) / "refs.bib").write_bytes(b"tampered")
    with pytest.raises(StagingError, match="differs"):
        verify_staging(project, manifest, workspace)


def test_verification_detects_a_changed_source(project: Path, workspace: Workspace):
    manifest = stage_files(project, [("refs.bib", "bibliography")], workspace)
    (project / "refs.bib").write_bytes(b"changed after staging")
    (staging_dir(workspace) / "refs.bib").write_bytes(b"changed after staging")
    with pytest.raises(StagingError):
        verify_staging(project, manifest, workspace)


def test_manifest_is_written_inside_the_workspace_without_absolute_paths(
    project: Path, workspace: Workspace
):
    manifest = stage_files(project, [("main.tex", "main")], workspace)
    path = write_staging_manifest(manifest, workspace)
    assert path.parent == workspace.path
    assert str(project) not in path.read_text(encoding="utf-8")
    assert isinstance(manifest, StagingManifest)
