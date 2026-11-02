"""Helpers for tests that run the real Pandoc (skipped when it is not installed)."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from latex2word.profiles import load_profile

PANDOC = shutil.which("pandoc")
requires_pandoc = pytest.mark.skipif(PANDOC is None, reason="pandoc is not installed")
FILTER = Path(__file__).resolve().parents[1] / "src" / "latex2word" / "filters" / "l2w_semantic.lua"

PREAMBLE = "\\documentclass{article}\n\\usepackage{amsmath,graphicx,booktabs}\n\\begin{document}\n"
POSTAMBLE = "\n\\end{document}\n"


def document(body: str) -> str:
    return PREAMBLE + body + POSTAMBLE


def pandoc_json(tmp_path: Path, latex: str, *args: str) -> dict[str, Any]:
    """The Pandoc JSON AST of ``latex`` (default reader options)."""
    source = tmp_path / "ast.tex"
    source.write_text(latex, encoding="utf-8")
    completed = subprocess.run(
        [PANDOC, "-f", "latex", "-t", "json", *args, source.name],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return json.loads(completed.stdout)


def filter_config(
    profile: str = "generic",
    *,
    mode: str = "citeproc",
    report: Path,
    source_labels: Sequence[str] | None = None,
    numbering_overrides: Mapping[str, Any] | None = None,
    search_dirs: Sequence[Path] = (),
) -> dict[str, Any]:
    resolved = load_profile(profile, search_dirs)
    numbering = json.loads(json.dumps(resolved.numbering))
    for key, value in (numbering_overrides or {}).items():
        kind, _, field = key.partition(".")
        numbering[kind][field] = value
    config: dict[str, Any] = {
        "report_path": report.as_posix(),
        "numbering": numbering,
        "bibliography": {"mode": mode, "heading": "References", "manual": resolved.bibliography["manual"]},
    }
    if source_labels is not None:
        config["source_labels"] = list(source_labels)
    return config


def run_filter(
    tmp_path: Path,
    latex: str,
    *,
    profile: str = "generic",
    to: str = "json",
    output: str | None = None,
    **config_kwargs: Any,
) -> tuple[Any, dict[str, Any]]:
    """Run Pandoc with the latex2word filter; returns ``(AST json or output path, filter report)``."""
    source = tmp_path / "main.tex"
    source.write_text(latex, encoding="utf-8")
    report_path = tmp_path / "report.json"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(filter_config(profile, report=report_path, **config_kwargs)), encoding="utf-8")
    argv = [PANDOC, "-f", "latex", "-t", to, "--lua-filter", str(FILTER), "-M", f"l2w-config={config_path.as_posix()}"]
    if output:
        argv += ["-o", output]
    completed = subprocess.run(argv + [source.name], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    for key in ("labels", "refs", "citations", "raw_tex", "unnumbered_headings", "diagnostics"):
        if isinstance(report.get(key), dict):
            report[key] = list(report[key].values())
    for key, value in list(report["sequences"].items()):
        if isinstance(value, dict):
            report["sequences"][key] = list(value.values())
    ast = json.loads(completed.stdout) if output is None else tmp_path / output
    return ast, report


def walk(node: Any) -> Iterator[dict[str, Any]]:
    """Every AST element (dict with a ``t`` tag) below ``node`` in document order."""
    if isinstance(node, dict):
        if "t" in node:
            yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def text_of(node: Any) -> str:
    out: list[str] = []
    for element in walk(node):
        tag = element["t"]
        if tag == "Str":
            out.append(element["c"])
        elif tag in ("Space", "SoftBreak"):
            out.append(" ")
        elif tag == "LineBreak":
            out.append("\n")
    return "".join(out)


def blocks_of(ast: Mapping[str, Any], tag: str) -> list[dict[str, Any]]:
    return [b for b in ast["blocks"] if b["t"] == tag]


def codes(report: Mapping[str, Any]) -> list[str]:
    return [d["code"] for d in report["diagnostics"]]
