"""Numeral styles and number-format templates shared by profiles and the DOCX validator.

The Pandoc filter (``filters/l2w_numerals.lua``) renders numbers; this module is
its Python counterpart: it validates profile templates and can *parse* rendered
numbers back, so the validator cross-checks the filter instead of trusting it.

Templates are strings with ``{placeholder}`` fields:

* ``{n}``                      the counter of this kind in its configured numeral style;
* ``{arabic}`` ``{Roman}`` ``{roman}`` ``{Alpha}`` ``{alpha}``   the same counter in a forced style;
* ``{n1}`` ``{n2}`` ``{n3}``   the current section counters, each in its own level's style.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import cache

NUMERAL_STYLES: tuple[str, ...] = ("arabic", "Roman", "roman", "Alpha", "alpha")
FORCED_STYLE_FIELDS: tuple[str, ...] = NUMERAL_STYLES
SECTION_FIELDS: tuple[str, ...] = ("n1", "n2", "n3")
MAX_ROMAN = 3999
MAX_ALPHA = 26

_FIELD = re.compile(r"\{([^{}]*)\}")
_ROMAN = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)


class NumeralError(ValueError):
    """A number cannot be written in the requested style."""


def format_numeral(n: int, style: str) -> str:
    """Write ``n`` (>= 1) in ``style``; raises :class:`NumeralError` when it cannot be represented."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise NumeralError(f"counter value must be a positive integer, got {n!r}")
    if style == "arabic":
        return str(n)
    if style in ("Roman", "roman"):
        if n > MAX_ROMAN:
            raise NumeralError(f"{n} exceeds the Roman numeral range (1..{MAX_ROMAN})")
        remaining, out = n, []
        for value, glyph in _ROMAN:
            while remaining >= value:
                out.append(glyph)
                remaining -= value
        text = "".join(out)
        return text if style == "Roman" else text.lower()
    if style in ("Alpha", "alpha"):
        if n > MAX_ALPHA:
            raise NumeralError(f"{n} exceeds the alphabetic range (1..{MAX_ALPHA})")
        letter = chr(ord("A") + n - 1)
        return letter if style == "Alpha" else letter.lower()
    raise NumeralError(f"unknown numeral style {style!r}")


def numeral_pattern(style: str) -> str:
    """A regular expression (no groups) matching any numeral written in ``style``."""
    if style == "arabic":
        return r"[0-9]+"
    if style == "Roman":
        return r"[IVXLCDM]+"
    if style == "roman":
        return r"[ivxlcdm]+"
    if style == "Alpha":
        return r"[A-Z]"
    if style == "alpha":
        return r"[a-z]"
    raise NumeralError(f"unknown numeral style {style!r}")


def parse_numeral(text: str, style: str) -> int | None:
    """Inverse of :func:`format_numeral`; ``None`` for text that is not a canonical numeral."""
    if not re.fullmatch(numeral_pattern(style), text):
        return None
    if style == "arabic":
        value = int(text)
        return value if value >= 1 and str(value) == text else None
    if style in ("Alpha", "alpha"):
        return ord(text.upper()) - ord("A") + 1
    value = 0
    rest = text.upper()
    for amount, glyph in _ROMAN:
        while rest.startswith(glyph):
            value += amount
            rest = rest[len(glyph):]
    if rest or value < 1 or format_numeral(value, style) != text:
        return None
    return value


def template_fields(template: str) -> list[str]:
    """Names of the ``{fields}`` used by ``template`` in order of appearance."""
    return _FIELD.findall(template)


def template_problems(template: str, allowed: Iterable[str]) -> list[str]:
    """Human-readable problems of ``template``: unknown fields, stray braces, missing ``{n}``-like field."""
    allowed_set = set(allowed)
    problems: list[str] = []
    for name in template_fields(template):
        if name not in allowed_set:
            problems.append(f"unknown field {{{name}}} (allowed: {', '.join('{' + a + '}' for a in sorted(allowed_set))})")
    if _FIELD.sub("", template).count("{") or _FIELD.sub("", template).count("}"):
        problems.append("unbalanced or nested braces")
    return problems


def render_template(template: str, values: Mapping[str, str]) -> str:
    """Substitute ``{field}`` occurrences from ``values`` (every field must be present)."""
    return _FIELD.sub(lambda m: values[m.group(1)], template)


@cache
def _regex_source(template: str, styles: tuple[tuple[str, str], ...]) -> str:
    style_of = dict(styles)
    out: list[str] = []
    position = 0
    seen: dict[str, int] = {}
    for match in _FIELD.finditer(template):
        out.append(_literal(template[position : match.start()]))
        name = match.group(1)
        style = name if name in FORCED_STYLE_FIELDS else style_of[name]
        seen[name] = seen.get(name, 0) + 1
        group = name if seen[name] == 1 else f"{name}__{seen[name]}"
        out.append(f"(?P<{group}>{numeral_pattern(style)})")
        position = match.end()
    out.append(_literal(template[position:]))
    return "".join(out)


def _literal(text: str) -> str:
    return "".join(r"\s+" if ch in " \t\n\u00a0" else re.escape(ch) for ch in text)


def template_regex(template: str, field_styles: Mapping[str, str]) -> re.Pattern[str]:
    """Compile ``template`` into a regex whose named groups capture each numeral field.

    ``field_styles`` maps ``n``/``n1``/``n2``/``n3`` to numeral styles; forced-style
    fields (``{Roman}`` ...) carry their own. The first occurrence of a field is the
    group ``name``; later occurrences are ``name__2`` ... Whitespace matches any
    whitespace run.
    """
    return re.compile(_regex_source(template, tuple(sorted(field_styles.items()))))
