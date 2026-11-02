"""TikZ / circuitikz extraction and render-preamble derivation.

Extraction is environment- and brace-aware (see :func:`latex_scan.scan_environments`);
it never uses a lazy ``\\begin{env}.*?\\end{env}`` regex, so nested content cannot
terminate an environment early.

The render preamble is *derived* from the project's own preamble: only packages,
TikZ libraries and settings relevant to drawing are carried over, plus macro
definitions that the drawing actually uses. ``fontenc`` is deliberately never
carried over: with T1 the DVI refers to EC bitmap fonts that dvisvgm cannot turn
into vector glyphs (a lesson from an earlier pipeline); the render wrapper loads ``lmodern``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from latex2word.latex_scan import (
    LineIndex,
    iter_commands,
    mask_non_live,
    read_group,
    read_optional,
    scan_environments,
    skip_ws,
    split_top_level,
)

TIKZ_ENVIRONMENTS: tuple[str, ...] = ("tikzpicture", "circuitikz")

# Packages whose \usepackage line is carried into the render preamble.
RENDER_PACKAGES = frozenset(
    {"tikz", "circuitikz", "pgf", "pgfplots", "xcolor", "amsmath", "amssymb", "amsfonts", "mathtools", "siunitx"}
)
# Commands copied verbatim (with their brace groups) from the project preamble.
_VERBATIM_SETTINGS: dict[str, int] = {
    "usetikzlibrary": 1,
    "usepgfplotslibrary": 1,
    "tikzset": 1,
    "ctikzset": 1,
    "pgfplotsset": 1,
    "definecolor": 3,
    "colorlet": 2,
    # siunitx configuration: drawings use \SI{..}{\projectunit} like the body text does
    "sisetup": 1,
    "DeclareSIUnit": 2,
}
# Settings whose first argument may be preceded by an optional [..] group.
_OPTIONAL_FIRST = frozenset({"colorlet", "DeclareSIUnit"})
_MACRO_DEFINERS = frozenset({"newcommand", "renewcommand", "providecommand", "DeclareMathOperator"})
_BEGIN_DOCUMENT = re.compile(r"\\begin\s*\{document\}")


class TikzExtractionError(ValueError):
    """The TikZ/circuitikz environment could not be extracted unambiguously."""

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(f"line {line}: {message}" if line else message)
        self.line = line


@dataclass(frozen=True)
class ExtractedEnvironment:
    name: str
    source: str  # exact text from \begin{name} through \end{name}
    begin: int
    end: int
    line: int


def extract_environments(text: str, names: Sequence[str] = TIKZ_ENVIRONMENTS) -> list[ExtractedEnvironment]:
    """Extract every top-level environment named in ``names`` from raw LaTeX ``text``.

    Comments are ignored when locating ``\\begin``/``\\end`` but are preserved in
    the returned source. Raises :class:`TikzExtractionError` for unclosed,
    stray or mismatched environments.
    """
    masked = mask_non_live(text).text
    environments, problems = scan_environments(masked, names)
    if problems:
        first = problems[0]
        raise TikzExtractionError(first.message, LineIndex(text).line(first.offset))
    lines = LineIndex(text)
    return [
        ExtractedEnvironment(e.name, text[e.begin : e.end], e.begin, e.end, lines.line(e.begin)) for e in environments
    ]


def extract_environment(text: str, name: str, occurrence: int = 0) -> ExtractedEnvironment:
    """The ``occurrence``-th (0-based) top-level ``name`` environment of ``text``."""
    matches = [e for e in extract_environments(text, (name,))]
    if occurrence >= len(matches):
        raise TikzExtractionError(f"environment {name!r} occurrence {occurrence} not found ({len(matches)} present)")
    return matches[occurrence]


# ---- render preamble ------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderPreamble:
    """Derived preamble lines and the packages they load."""

    lines: tuple[str, ...]
    packages: frozenset[str]


@dataclass(frozen=True)
class _MacroDefinition:
    name: str
    text: str


def _take_groups(masked: str, text: str, start: int, pos: int, count: int) -> str | None:
    """Text from ``start`` through ``count`` brace groups beginning at ``pos`` (original text)."""
    end = pos
    for _ in range(count):
        end = skip_ws(masked, end)
        group = read_group(masked, end)
        if group is None:
            return None
        end = group[2]
    return text[start:end]


def _macro_definition(masked: str, text: str, command_name: str, start: int, pos: int) -> _MacroDefinition | None:
    pos = skip_ws(masked, pos)
    # name: {\foo} or \foo
    if masked[pos : pos + 1] == "{":
        group = read_group(masked, pos)
        if group is None:
            return None
        name = masked[group[0] : group[1]].strip()
        pos = group[2]
    else:
        match = re.compile(r"\\[A-Za-z@]+|\\.").match(masked, pos)
        if match is None:
            return None
        name = match.group(0)
        pos = match.end()
    if not name.startswith("\\"):
        return None
    if command_name == "DeclareMathOperator":
        body = _take_groups(masked, text, start, pos, 1)
        return _MacroDefinition(name, body) if body else None
    pos = skip_ws(masked, pos)
    while masked[pos : pos + 1] == "[":  # argument count and default
        optional = read_optional(masked, pos)
        if optional is None:
            return None
        pos = skip_ws(masked, optional[2])
    body = _take_groups(masked, text, start, pos, 1)
    return _MacroDefinition(name, body) if body else None


def _uses(name: str, haystack: str) -> bool:
    return re.search(re.escape(name) + r"(?![A-Za-z@])", haystack) is not None


def derive_render_preamble(main_text: str, drawing: str) -> RenderPreamble:
    """Derive the render preamble for ``drawing`` from the main file's preamble.

    ``main_text`` is the raw text of the main TeX file. Only the part before
    ``\\begin{document}`` is examined.
    """
    masked = mask_non_live(main_text).text
    document = _BEGIN_DOCUMENT.search(masked)
    limit = document.start() if document else len(masked)
    preamble_masked = masked[:limit]

    lines: list[str] = []
    packages: set[str] = set()
    macros: list[_MacroDefinition] = []

    for command in iter_commands(preamble_masked):
        name = command.name
        if name == "usepackage":
            pos = skip_ws(preamble_masked, command.end)
            options = ""
            optional = read_optional(preamble_masked, pos)
            if optional is not None:
                options = main_text[optional[0] : optional[1]].strip()
                pos = skip_ws(preamble_masked, optional[2])
            group = read_group(preamble_masked, pos)
            if group is None:
                continue
            for package in split_top_level(preamble_masked[group[0] : group[1]]):
                if package in RENDER_PACKAGES:
                    prefix = f"[{options}]" if options else ""
                    lines.append(f"\\usepackage{prefix}{{{package}}}")
                    packages.add(package)
        elif name in _VERBATIM_SETTINGS:
            pos = command.end
            if name in _OPTIONAL_FIRST:
                optional = read_optional(preamble_masked, skip_ws(preamble_masked, pos))
                if optional is not None:
                    pos = optional[2]
            snippet = _take_groups(preamble_masked, main_text, command.start, pos, _VERBATIM_SETTINGS[name])
            if snippet is not None:
                lines.append(" ".join(snippet.split()) if "\n" not in snippet.strip() else snippet.strip())
        elif name in _MACRO_DEFINERS:
            definition = _macro_definition(preamble_masked, main_text, name, command.start, command.end)
            if definition is not None:
                macros.append(definition)

    # keep only macro definitions the drawing uses (directly or through other kept macros)
    kept: list[_MacroDefinition] = []
    pending = list(macros)
    changed = True
    haystack = drawing
    while changed:
        changed = False
        for definition in list(pending):
            if _uses(definition.name, haystack):
                kept.append(definition)
                pending.remove(definition)
                haystack += "\n" + definition.text
                changed = True
    order = {id(m): i for i, m in enumerate(macros)}
    kept.sort(key=lambda m: order[id(m)])
    lines.extend(m.text.strip() for m in kept)
    return RenderPreamble(tuple(lines), frozenset(packages))


def build_render_document(
    drawing: str,
    preamble: RenderPreamble,
    environment: str,
    extra_preamble: Sequence[str] = (),
) -> str:
    """The standalone LaTeX wrapper that renders ``drawing`` to DVI for dvisvgm.

    ``pgfsys-dvisvgm.def`` makes TikZ emit dvisvgm specials; ``lmodern`` supplies
    scalable Latin Modern fonts (no EC bitmap fonts, see module docstring).
    Required drawing packages missing from the project preamble are added.
    """
    lines = list(preamble.lines)
    needed = "circuitikz" if environment == "circuitikz" else "tikz"
    if needed not in preamble.packages and not (needed == "tikz" and "circuitikz" in preamble.packages):
        lines.insert(0, f"\\usepackage{{{needed}}}")
    header = [
        "\\documentclass[border=2pt]{standalone}",
        "\\def\\pgfsysdriver{pgfsys-dvisvgm.def}",
        "\\usepackage{lmodern}",
        *lines,
        *extra_preamble,
        "\\begin{document}",
        drawing,
        "\\end{document}",
        "",
    ]
    return "\n".join(header)
