"""Output-target safety (audit finding F-06): the DOCX may never replace an input.

The build writes exactly one file. Before any conversion work starts, the target is compared,
canonically, with every file the build reads:

* every resolved node of the source graph (main file, ``\\input``/``\\include`` files, local
  ``.sty``/``.cls``, bibliography files, graphics, pgfplots data);
* the configuration file, the configured CSL file, ``staging.extra_files`` and the profile's
  external Word template (``reference_docx.path``, wherever it lies).

Canonical comparison: absolute path, ``..`` collapsed, symlinks/junctions resolved
(``os.path.realpath``), and letter case folded where the file system is case-insensitive
(Windows, macOS default). The rules:

``L2W-E-OUTPUT-001``  the target name does not end in ``.docx``;
``L2W-E-OUTPUT-002``  the target is (canonically) one of the inputs above;
``L2W-E-OUTPUT-003``  the target exists and is not a regular file holding a ZIP package
                      (an existing non-DOCX file is never overwritten);
``L2W-E-OUTPUT-004``  the target lies inside the project root but outside the configured output
                      directory (generated files go to the designated directory only);
``L2W-E-OUTPUT-005``  the configured output directory contains source files: it is not a
                      generated-output directory, so nothing is written there.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from pathlib import Path

from latex2word.diagnostics import Severity
from latex2word.source_graph import Finding, SourceGraph

_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")  # local file header; empty archive


def default_case_insensitive() -> bool:
    return os.name == "nt" or sys.platform == "darwin"


def canonical(path: Path | str, *, case_insensitive: bool | None = None) -> str:
    """The comparison key of ``path``: absolute, ``..`` collapsed, links resolved, case folded if needed."""
    fold = default_case_insensitive() if case_insensitive is None else case_insensitive
    key = os.path.realpath(os.path.abspath(os.fspath(path)))
    return os.path.normcase(key).casefold() if fold else key


def _inside(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def protected_inputs(graph: SourceGraph, extra: Iterable[Path] = ()) -> list[Path]:
    """Absolute paths of every resolved source-graph node, plus ``extra`` (config, CSL, extra files)."""
    root = Path(graph.root)
    paths = [root / Path(*rel.split("/")) for rel, _kind in graph.stageable_files()]
    return [*paths, *(Path(p) for p in extra)]


def _finding(number: int, message: str) -> Finding:
    return Finding(f"L2W-E-OUTPUT-{number:03d}", Severity.ERROR, message)


def check_output_target(
    target: Path,
    *,
    project_root: Path,
    output_directory: Path,
    inputs: Iterable[Path],
    case_insensitive: bool | None = None,
) -> list[Finding]:
    """Every reason why writing the DOCX to ``target`` would be unsafe (empty list: safe)."""
    def key(p: Path | str) -> str:
        return canonical(p, case_insensitive=case_insensitive)

    findings: list[Finding] = []
    shown = os.fspath(target)
    if not Path(target).name.lower().endswith(".docx"):
        findings.append(_finding(1, f"output target {shown!r} is not a .docx file"))

    target_key = key(target)
    input_list = list(inputs)
    for source in input_list:
        if key(source) == target_key:
            findings.append(_finding(2, f"output target {shown!r} is an input of the build ({os.fspath(source)}); refusing to overwrite it"))
            break

    resolved = Path(os.path.realpath(os.path.abspath(shown)))
    if os.path.lexists(shown):
        if not resolved.is_file():
            findings.append(_finding(3, f"output target {shown!r} exists and is not a regular file"))
        else:
            try:
                with resolved.open("rb") as handle:
                    head = handle.read(4)
            except OSError as exc:
                findings.append(_finding(3, f"output target {shown!r} exists and cannot be read: {exc}"))
            else:
                if head and head not in _ZIP_MAGIC:
                    findings.append(_finding(3, f"output target {shown!r} exists and is not a DOCX package; refusing to overwrite it"))

    root_key, out_key = key(project_root), key(output_directory)
    if _inside(target_key, root_key):
        if not _inside(target_key, out_key) or out_key == root_key:
            findings.append(_finding(4, f"output target {shown!r} is inside the project but outside the configured output directory"))
        else:
            overlap = sorted(os.fspath(s) for s in input_list if _inside(key(s), out_key))
            if overlap:
                findings.append(_finding(5, f"the output directory {os.fspath(output_directory)!r} contains build inputs ({overlap[0]}); it is not a generated-output directory"))
    return findings
