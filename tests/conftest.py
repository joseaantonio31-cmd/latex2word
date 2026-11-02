"""Shared fixtures: fake PATH lookup and fake command runner (no real tools needed)."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from latex2word.process import CommandResult

REPO_ROOT = Path(__file__).resolve().parents[1]

# Person- or machine-specific strings that must never appear in production code (user names, private project
# names...). They are supplied locally, never committed: LATEX2WORD_PRIVATE_MARKERS="name1,name2".
PRIVATE_MARKERS = tuple(
    marker.strip().lower() for marker in os.environ.get("LATEX2WORD_PRIVATE_MARKERS", "").split(",") if marker.strip()
)


def make_result(
    args: tuple[str, ...] = ("tool",),
    *,
    returncode: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    error: str | None = None,
) -> CommandResult:
    return CommandResult(args, returncode, stdout, stderr, timed_out, 0.0, error)


class FakeRunner:
    """Records calls; answers by executable path (default: ``fake 1.0``)."""

    def __init__(self, results: Mapping[str, CommandResult] | None = None) -> None:
        self.results = dict(results or {})
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: tuple[str, ...], *, timeout: float) -> CommandResult:
        self.calls.append(tuple(args))
        assert timeout > 0
        return self.results.get(args[0], make_result(tuple(args), stdout=f"{Path(args[0]).stem} 1.0\n"))


def fake_which(present: set[str] | Mapping[str, str]) -> Callable[[str], str | None]:
    """A ``shutil.which`` replacement that finds only the names in ``present``."""
    table = dict(present) if isinstance(present, Mapping) else {name: f"/fake/bin/{name}" for name in present}
    return table.get


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


def certified_pandoc_runner(version: str = "3.12") -> FakeRunner:
    """A fake runner whose ``/fake/bin/pandoc`` reports ``pandoc <version>`` (default: the certified version).

    Doctor tests built on it pass ``probe=False``: the Pandoc capability probe runs real conversions that a fake
    runner cannot answer; the probe and the compatibility classes have their own tests (``test_pandoc_compat.py``).
    """
    return FakeRunner({"/fake/bin/pandoc": make_result(("/fake/bin/pandoc",), stdout=f"pandoc {version}\n")})


ALL_TOOLS = {
    "pandoc",
    "latex",
    "pdflatex",
    "dvisvgm",
    "inkscape",
    "rsvg-convert",
    "pdftocairo",
    "pandoc-tex-numbering",
}
