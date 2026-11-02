"""Expand siunitx commands in the staged copy into text and math Pandoc converts faithfully.

Pandoc's own siunitx support is partial and loses content silently: units
declared with ``\\DeclareSIUnit`` vanish (``\\SI{5}{\\dBm}`` -> ``5``), some
prefixed units vanish (``\\giga\\sample``), ``\\decibel`` becomes ``db``,
literal units with subscripts are garbled, and inside math (``$P>\\SI{10}{\\dBm}$``)
the formula is not converted at all. This pass replaces every siunitx command of
the document body by an equivalent the reader handles:

* in text: the number and unit as plain text (no-break space between them,
  ``\\textsuperscript`` / ``\\textsubscript`` for powers and qualifiers, U+2212 minus);
* in math: ``\\mathrm{...}`` units with TeX powers, which become native Word equations.

The project's ``\\sisetup`` (per-mode, range/list phrases and units, decimal
marker, digit grouping, products) and ``\\DeclareSIUnit`` definitions are
honoured; list and range words follow the main babel language. A command that
cannot be parsed (unknown unit macro, unreadable number) is left untouched and
reported, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from latex2word.diagnostics import Severity
from latex2word.latex_scan import iter_commands, read_group, read_optional, skip_ws, split_top_level
from latex2word.rewrite import Edit
from latex2word.source_graph import Finding, SourceGraph, SourceLocation
from latex2word.texprobe import mask_math

NBSP = "\u00a0"
MINUS = "\u2212"

PREFIXES: dict[str, str] = {
    "quecto": "q", "ronto": "r", "yocto": "y", "zepto": "z", "atto": "a", "femto": "f", "pico": "p", "nano": "n",
    "micro": "\u00b5", "milli": "m", "centi": "c", "deci": "d", "deca": "da", "deka": "da", "hecto": "h",
    "kilo": "k", "mega": "M", "giga": "G", "tera": "T", "peta": "P", "exa": "E", "zetta": "Z", "yotta": "Y",
    "ronna": "R", "quetta": "Q", "kibi": "Ki", "mebi": "Mi", "gibi": "Gi", "tebi": "Ti",
}
UNITS: dict[str, str] = {
    "ampere": "A", "candela": "cd", "kelvin": "K", "kilogram": "kg", "gram": "g", "metre": "m", "meter": "m",
    "mole": "mol", "second": "s", "becquerel": "Bq", "degreeCelsius": "\u00b0C", "celsius": "\u00b0C",
    "coulomb": "C", "farad": "F", "gray": "Gy", "hertz": "Hz", "henry": "H", "joule": "J", "katal": "kat",
    "lumen": "lm", "lux": "lx", "newton": "N", "ohm": "\u03a9", "pascal": "Pa", "radian": "rad", "siemens": "S",
    "sievert": "Sv", "steradian": "sr", "tesla": "T", "volt": "V", "watt": "W", "weber": "Wb", "day": "d",
    "hectare": "ha", "hour": "h", "litre": "L", "liter": "L", "minute": "min", "tonne": "t",
    "astronomicalunit": "au", "bel": "B", "decibel": "dB", "neper": "Np", "dalton": "Da", "electronvolt": "eV",
    "degree": "\u00b0", "arcminute": "\u2032", "arcsecond": "\u2033", "percent": "%", "angstrom": "\u00c5",
    "bar": "bar", "barn": "b", "knot": "kn", "mmHg": "mmHg", "nauticalmile": "M", "bit": "bit", "byte": "B",
    "baud": "Bd", "atomicmassunit": "u", "bohr": "a\u2080", "clight": "c", "planckbar": "\u0127",
    "electronmass": "m\u2091", "hartree": "E\u2095", "elementarycharge": "e", "neper": "Np",
}
# siunitx abbreviations: prefix + unit in one macro
ABBREVIATIONS: dict[str, str] = {}
for _p, _sym in (("", ""), ("f", "f"), ("p", "p"), ("n", "n"), ("u", "\u00b5"), ("m", "m"), ("k", "k"), ("M", "M"),
                 ("G", "G"), ("T", "T")):
    for _u, _usym in (("A", "A"), ("V", "V"), ("W", "W"), ("J", "J"), ("s", "s"), ("m", "m"), ("g", "g"), ("L", "L"),
                      ("F", "F"), ("H", "H"), ("Hz", "Hz"), ("N", "N"), ("Pa", "Pa"), ("C", "C"), ("eV", "eV"),
                      ("mol", "mol"), ("K", "K")):
        ABBREVIATIONS[_p + _u] = _sym + _usym
ABBREVIATIONS.update({
    "ohm": "\u03a9", "kohm": "k\u03a9", "Mohm": "M\u03a9", "dB": "dB", "kWh": "kWh", "cd": "cd", "Bq": "Bq",
    "Gy": "Gy", "Sv": "Sv", "S": "S", "mS": "mS", "uS": "\u00b5S", "Np": "Np", "mmHg": "mmHg",
})
# arc units: no space between number and unit
NO_SPACE_SYMBOLS = frozenset({"\u00b0", "\u2032", "\u2033"})

LANGUAGE_WORDS: dict[str, dict[str, str]] = {
    "english": {"and": " and ", "to": " to "},
    "spanish": {"and": " y ", "to": " a "},
    "french": {"and": " et ", "to": " \u00e0 "},
    "german": {"and": " und ", "to": " bis "},
    "ngerman": {"and": " und ", "to": " bis "},
    "portuguese": {"and": " e ", "to": " a "},
    "brazilian": {"and": " e ", "to": " a "},
    "italian": {"and": " e ", "to": " a "},
    "catalan": {"and": " i ", "to": " a "},
}
_BABEL_ALIASES = {"american": "english", "british": "english", "UKenglish": "english", "USenglish": "english",
                  "english": "english", "spanish": "spanish", "castilian": "spanish", "french": "french",
                  "francais": "french", "german": "german", "ngerman": "ngerman", "portuguese": "portuguese",
                  "brazil": "brazilian", "brazilian": "brazilian", "italian": "italian", "catalan": "catalan"}

COMMAND_ARGS: dict[str, int] = {
    "SI": 2, "qty": 2, "si": 1, "unit": 1, "num": 1, "SIrange": 3, "qtyrange": 3, "numrange": 2,
    "SIlist": 2, "qtylist": 2, "numlist": 1, "ang": 1,
}
_TEXT_IN_MATH = frozenset({"text", "textrm", "textnormal", "mbox", "textit", "textbf", "textup", "hbox"})
_BEGIN_DOCUMENT = re.compile(r"\\begin\s*\{document\}")


class UnitError(ValueError):
    """The siunitx input cannot be expanded faithfully."""


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class Settings:
    per_mode: str = "power"  # power | symbol | fraction (fraction rendered as symbol)
    range_phrase: str | None = None  # None: language word
    range_units: str = "repeat"  # repeat | single | brackets
    list_units: str = "repeat"
    list_separator: str = ", "
    list_final_separator: str | None = None
    list_pair_separator: str | None = None
    decimal_marker: str = "."
    group_digits: bool = True
    group_minimum_digits: int = 5
    separate_uncertainty: bool = False
    words: dict[str, str] = field(default_factory=lambda: dict(LANGUAGE_WORDS["english"]))

    def final_separator(self) -> str:
        return self.list_final_separator if self.list_final_separator is not None else self.words["and"]

    def pair_separator(self) -> str:
        if self.list_pair_separator is not None:
            return self.list_pair_separator
        return self.final_separator()

    def phrase(self) -> str:
        return self.range_phrase if self.range_phrase is not None else self.words["to"]


def _tex_value(value: str) -> str:
    """A simple TeX option value as text (``{--}`` -> en dash, ``\\ `` -> space, ``~`` -> no-break space)."""
    value = value.strip()
    while value.startswith("{") and value.endswith("}") and read_group(value, 0) == (1, len(value) - 1, len(value)):
        value = value[1:-1].strip() if not value[1:-1].startswith(" ") else value[1:-1]
    value = value.replace("---", "\u2014").replace("--", "\u2013").replace("\\ ", " ").replace("~", NBSP)
    value = value.replace("\\,", NBSP).replace("{}", "")
    return value


def apply_options(settings: Settings, text: str) -> Settings:
    """``settings`` updated by a ``key=value`` list (``\\sisetup`` or a command's optional argument)."""
    changes: dict[str, Any] = {}
    for item in split_top_level(text):
        key, equals, value = item.partition("=")
        key = key.strip()
        raw = value.strip()
        if key == "per-mode" and equals:
            changes["per_mode"] = raw if raw in ("power", "symbol", "fraction", "power-positive-first") else "power"
        elif key == "range-phrase" and equals:
            changes["range_phrase"] = _tex_value(raw)
        elif key == "range-units" and equals:
            changes["range_units"] = raw if raw in ("repeat", "single", "brackets") else "repeat"
        elif key == "list-units" and equals:
            changes["list_units"] = raw if raw in ("repeat", "single", "brackets") else "repeat"
        elif key == "list-separator" and equals:
            changes["list_separator"] = _tex_value(raw)
        elif key == "list-final-separator" and equals:
            changes["list_final_separator"] = _tex_value(raw)
        elif key == "list-pair-separator" and equals:
            changes["list_pair_separator"] = _tex_value(raw)
        elif key == "output-decimal-marker" and equals:
            changes["decimal_marker"] = _tex_value(raw) or "."
        elif key == "group-digits":
            changes["group_digits"] = raw not in ("none", "false")
        elif key == "group-minimum-digits" and raw.isdigit():
            changes["group_minimum_digits"] = int(raw)
        elif key == "separate-uncertainty":
            changes["separate_uncertainty"] = raw in ("", "true")
        elif key == "uncertainty-mode" and equals:
            changes["separate_uncertainty"] = raw == "separate"
    return replace(settings, **changes) if changes else settings


def babel_language(preamble_masked: str, preamble_text: str) -> str | None:
    """The main babel language (``main=`` or the last language option), or None."""
    language = None
    for command in iter_commands(preamble_masked):
        if command.name != "usepackage":
            continue
        pos = skip_ws(preamble_masked, command.end)
        optional = read_optional(preamble_masked, pos)
        if optional is None:
            continue
        group = read_group(preamble_masked, skip_ws(preamble_masked, optional[2]))
        if group is None or "babel" not in split_top_level(preamble_masked[group[0] : group[1]]):
            continue
        for option in split_top_level(preamble_text[optional[0] : optional[1]]):
            key, _, value = option.partition("=")
            if key.strip() == "main" and value.strip() in _BABEL_ALIASES:
                return _BABEL_ALIASES[value.strip()]
            if option.strip() in _BABEL_ALIASES:
                language = _BABEL_ALIASES[option.strip()]
    return language


# --------------------------------------------------------------------------- units


@dataclass
class _Factor:
    symbol: str  # e.g. "GSa", "\u00b0C", "V"
    power: int = 1
    qualifier: str | None = None  # subscript (\of{..} or a literal V_{pp})


class UnitParser:
    def __init__(self, declared: dict[str, str]) -> None:
        self.declared = declared  # macro name -> definition body

    def parse(self, unit: str, depth: int = 0) -> list[_Factor]:
        if depth > 8:
            raise UnitError("recursive unit definition")
        factors: list[_Factor] = []
        pending_prefix = ""
        pending_power: int | None = None
        per = False
        i, n = 0, len(unit)

        def add(factor: _Factor) -> None:
            nonlocal pending_prefix, pending_power, per
            factor.symbol = pending_prefix + factor.symbol
            if pending_power is not None:
                factor.power *= pending_power
            if per:
                factor.power = -factor.power
            factors.append(factor)
            pending_prefix, pending_power, per = "", None, False

        while i < n:
            ch = unit[i]
            if ch.isspace() or ch in ".~":
                i += 1
                continue
            if ch == "\\":
                j = i + 1
                while j < n and unit[j].isalpha():
                    j += 1
                name = unit[i + 1 : j]
                if not name:
                    if unit[j : j + 1] in (",", " ", ";"):
                        i = j + 1
                        continue
                    raise UnitError(f"unexpected {unit[i:j + 1]!r} in unit")
                i = j
                if name in PREFIXES:
                    pending_prefix += PREFIXES[name]
                elif name == "per":
                    per = True
                elif name in ("square", "cubic"):
                    pending_power = 2 if name == "square" else 3
                elif name == "raiseto":
                    group = read_group(unit, skip_ws(unit, i))
                    if group is None:
                        raise UnitError("\\raiseto without argument")
                    pending_power = _int_power(unit[group[0] : group[1]])
                    i = group[2]
                elif name in ("squared", "cubed", "tothe"):
                    if not factors:
                        raise UnitError(f"\\{name} without a unit")
                    if name == "tothe":
                        group = read_group(unit, skip_ws(unit, i))
                        if group is None:
                            raise UnitError("\\tothe without argument")
                        power = _int_power(unit[group[0] : group[1]])
                        i = group[2]
                    else:
                        power = 2 if name == "squared" else 3
                    factors[-1].power *= power
                elif name == "of":
                    group = read_group(unit, skip_ws(unit, i))
                    if group is None or not factors:
                        raise UnitError("\\of without argument or unit")
                    factors[-1].qualifier = unit[group[0] : group[1]].strip()
                    i = group[2]
                elif name in ("highlight", "cancel"):
                    group = read_group(unit, skip_ws(unit, i))
                    if group is not None and name == "highlight":
                        i = group[2]  # colour argument; the unit follows
                elif name in UNITS:
                    add(_Factor(UNITS[name]))
                elif name in ABBREVIATIONS:
                    add(_Factor(ABBREVIATIONS[name]))
                elif name in self.declared:
                    inner = self.parse(self.declared[name], depth + 1)
                    if len(inner) == 1:
                        add(inner[0])
                    else:  # a compound declared unit: the prefix applies to its first factor
                        if inner:
                            first = inner[0]
                            first.symbol = pending_prefix + first.symbol
                            pending_prefix = ""
                        for k, factor in enumerate(inner):
                            if per:
                                factor.power = -factor.power
                            factors.append(factor)
                        per = False
                else:
                    raise UnitError(f"unknown unit macro \\{name}")
                continue
            if ch == "/":
                per = True
                i += 1
                continue
            if ch in "^_":
                group = read_group(unit, i + 1)
                value = unit[group[0] : group[1]] if group else unit[i + 1 : i + 2]
                end = group[2] if group else i + 2
                if not factors:
                    raise UnitError(f"{ch} without a unit")
                if ch == "^":
                    factors[-1].power *= _int_power(value)
                else:
                    factors[-1].qualifier = value.strip()
                i = end
                continue
            if ch == "{":
                group = read_group(unit, i)
                if group is None:
                    raise UnitError("unbalanced braces in unit")
                for factor in self.parse(unit[group[0] : group[1]], depth + 1):
                    add(factor)
                i = group[2]
                continue
            # literal text: a run of letters/symbols up to the next special character
            j = i
            while j < n and not unit[j].isspace() and unit[j] not in "\\^_{}/.~":
                j += 1
            add(_Factor(unit[i:j]))
            i = j
        if pending_prefix or pending_power is not None or per:
            raise UnitError("dangling prefix, power or \\per at the end of the unit")
        return factors


def _int_power(text: str) -> int:
    text = text.strip().replace(MINUS, "-")
    if not re.fullmatch(r"[+-]?\d+", text):
        raise UnitError(f"unsupported power {text!r}")
    return int(text)


def _text_power(power: int) -> str:
    return "" if power == 1 else "\\textsuperscript{" + str(power).replace("-", MINUS) + "}"


_TEXT_SPECIALS = {"%": "\\%", "#": "\\#", "&": "\\&", "$": "\\$"}


def _escape_text(symbol: str) -> str:
    """Characters that TeX would read as markup (``%`` starts a comment) are escaped."""
    return "".join(_TEXT_SPECIALS.get(ch, ch) for ch in symbol)


def _factor_text(f: _Factor, power: int) -> str:
    out = _escape_text(f.symbol)
    if f.qualifier:
        out += "\\textsubscript{" + f.qualifier + "}"
    return out + _text_power(power)


# The degree sign stays a plain character inside \mathrm{} ("\u00b0C"): a {}^{\circ} script would need an empty
# base, which the converter fills with a zero-width space.
_MATH_SYMBOLS = {"\u00b5": "\\mu ", "\u03a9": "\\Omega ", "%": "\\%", "\u2032": "'", "\u2033": "''", "\u00c5": "\\AA "}


def _math_symbol(symbol: str) -> str:
    parts: list[str] = []
    plain: list[str] = []
    for ch in symbol:
        if ch in _MATH_SYMBOLS:
            if plain:
                parts.append("\\mathrm{" + "".join(plain) + "}")
                plain = []
            parts.append(_MATH_SYMBOLS[ch].strip() if ch not in "\u00b5\u03a9" else "\\mathrm{" + _MATH_SYMBOLS[ch].strip() + "}")
        else:
            plain.append(ch)
    if plain:
        parts.append("\\mathrm{" + "".join(plain) + "}")
    return "".join(parts)


def _factor_math(f: _Factor, power: int) -> str:
    out = _math_symbol(f.symbol)
    if f.qualifier:
        out = "{" + out + "}_{\\mathrm{" + f.qualifier + "}}"
    if power != 1:
        out = "{" + out + "}^{" + str(power) + "}"
    return out


def render_unit(factors: list[_Factor], settings: Settings, math: bool) -> str:
    product = "\\," if math else NBSP
    piece = _factor_math if math else _factor_text
    if settings.per_mode in ("symbol", "fraction") and any(f.power < 0 for f in factors) and any(f.power > 0 for f in factors):
        numerator = [piece(f, f.power) for f in factors if f.power > 0]
        denominator = [piece(f, -f.power) for f in factors if f.power < 0]
        top = product.join(numerator)
        bottom = product.join(denominator)
        if len(denominator) > 1:
            bottom = ("\\left(" + bottom + "\\right)") if math else "(" + bottom + ")"
        return f"{top}/{bottom}"
    return product.join(piece(f, f.power) for f in factors)


# --------------------------------------------------------------------------- numbers

_NUMBER = re.compile(
    r"^(?P<sign>[+\-]|\\pm|\\mp)?(?P<mant>\d*(?:[.,]\d*)?)(?:\s*(?:\\pm|\+-)\s*(?P<unc>\d*(?:[.,]\d*)?))?"
    r"(?:\((?P<bracket>\d+)\))?(?:[eEdD](?P<exp>[+\-]?\d+))?$"
)


def _group(digits: str, settings: Settings, math: bool = False) -> str:
    if not settings.group_digits or len(digits) < settings.group_minimum_digits:
        return digits
    out = []
    while len(digits) > 3:
        out.insert(0, digits[-3:])
        digits = digits[:-3]
    out.insert(0, digits)
    return ("\\," if math else NBSP).join(out)


def _mantissa(text: str, settings: Settings, math: bool = False) -> str:
    text = text.replace(",", ".")
    integer, dot, fraction = text.partition(".")
    out = _group(integer, settings, math) if integer else "0" if fraction else ""
    if dot:
        out += settings.decimal_marker + fraction
    return out


def render_number(raw: str, settings: Settings, math: bool) -> str:
    text = "".join(raw.split())
    m = _NUMBER.match(text)
    if m is None or not (m.group("mant") or m.group("exp")):
        raise UnitError(f"cannot read the number {raw.strip()!r}")
    sign = m.group("sign") or ""
    out = ""
    if sign == "-":
        out = "-" if math else MINUS
    elif sign == "+":
        out = "+"
    elif sign in ("\\pm", "\\mp"):
        out = (sign + " ") if math else ("\u00b1" if sign == "\\pm" else "\u2213")
    mantissa = m.group("mant")
    if mantissa:
        out += _mantissa(mantissa, settings, math)
    uncertainty = m.group("unc")
    bracket = m.group("bracket")
    if bracket and mantissa and "." in mantissa.replace(",", "."):
        # 1.23(4) -> 1.23 +- 0.04
        places = len(mantissa.replace(",", ".").partition(".")[2])
        uncertainty = "0." + bracket.rjust(places, "0") if len(bracket) <= places else bracket
    if uncertainty:
        pm = "\\pm " if math else f"{NBSP}\u00b1{NBSP}"
        out += pm + _mantissa(uncertainty, settings, math)
    exponent = m.group("exp")
    if exponent is None:
        return out
    value = str(int(exponent))
    power = ("10^{" + value + "}") if math else ("10\\textsuperscript{" + value.replace("-", MINUS) + "}")
    if not mantissa:
        return out[: len(out) - len(out.lstrip("+-" + MINUS))] + power if out else power
    if uncertainty:
        out = ("\\left(" + out + "\\right)") if math else "(" + out + ")"
    return out + ("\\times " if math else f"{NBSP}\u00d7{NBSP}") + power


# --------------------------------------------------------------------------- commands


@dataclass
class SiunitxPlan:
    edits: list[Edit] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    expanded: int = 0


@dataclass
class _Context:
    settings: Settings
    parser: UnitParser


def _join_list(items: list[str], settings: Settings) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return items[0] + settings.pair_separator() + items[1]
    return settings.list_separator.join(items[:-1]) + settings.final_separator() + items[-1]


def _number_unit(number: str, unit: str, factors: list[_Factor], math: bool) -> str:
    if not unit:
        return number
    if ("\\pm " in number[1:] if math else "±" in number[1:]) and not number.startswith(("(", "\\left(")):
        number = ("\\left(" + number + "\\right)") if math else "(" + number + ")"
    if factors and factors[0].symbol in NO_SPACE_SYMBOLS and factors[0].power == 1 and len(factors) == 1:
        return number + unit
    return number + ("\\," if math else NBSP) + unit


def expand_command(name: str, args: list[str], options: str | None, ctx: _Context, math: bool) -> str:
    settings = apply_options(ctx.settings, options) if options else ctx.settings
    if name in ("si", "unit"):
        return render_unit(ctx.parser.parse(args[0]), settings, math)
    if name == "num":
        return render_number(args[0], settings, math)
    if name in ("SI", "qty"):
        factors = ctx.parser.parse(args[1])
        return _number_unit(render_number(args[0], settings, math), render_unit(factors, settings, math), factors, math)
    if name in ("SIrange", "qtyrange", "numrange"):
        factors = ctx.parser.parse(args[2]) if name != "numrange" else []
        unit = render_unit(factors, settings, math) if factors else ""
        low, high = (render_number(a, settings, math) for a in args[:2])
        phrase = settings.phrase()
        if math:
            phrase = "\\text{" + phrase + "}"
        if not unit:
            return low + phrase + high
        if settings.range_units == "single":
            return _number_unit(low + phrase + high, unit, factors, math)
        if settings.range_units == "brackets":
            left, right = ("\\left(", "\\right)") if math else ("(", ")")
            return _number_unit(left + low + phrase + high + right, unit, factors, math)
        return _number_unit(low, unit, factors, math) + phrase + _number_unit(high, unit, factors, math)
    if name in ("SIlist", "qtylist", "numlist"):
        factors = ctx.parser.parse(args[1]) if name != "numlist" else []
        unit = render_unit(factors, settings, math) if factors else ""
        numbers = [render_number(item, settings, math) for item in args[0].split(";") if item.strip()]
        if not numbers:
            raise UnitError("empty list")
        if math:
            settings = replace(settings, list_separator=",\\ ", list_final_separator="\\text{" + settings.final_separator() + "}",
                               list_pair_separator="\\text{" + settings.pair_separator() + "}")
        if not unit:
            return _join_list(numbers, settings)
        if settings.list_units == "single":
            return _number_unit(_join_list(numbers, settings), unit, factors, math)
        if settings.list_units == "brackets":
            left, right = ("\\left(", "\\right)") if math else ("(", ")")
            return _number_unit(left + _join_list(numbers, settings) + right, unit, factors, math)
        return _join_list([_number_unit(n, unit, factors, math) for n in numbers], settings)
    if name == "ang":
        parts = [p for p in args[0].split(";")]
        marks = ["\u00b0", "\u2032", "\u2033"] if not math else ["^{\\circ}", "'", "''"]
        out = "".join(render_number(p, settings, math) + marks[k] for k, p in enumerate(parts) if p.strip())
        if not out:
            raise UnitError("empty angle")
        return out
    raise UnitError(f"unsupported command \\{name}")


def _text_in_math_spans(masked: str, math_masked: str) -> list[tuple[int, int]]:
    """Groups of ``\\text{..}``-like commands that sit inside math (their content is text again)."""
    spans: list[tuple[int, int]] = []
    for command in iter_commands(masked):
        if command.name in _TEXT_IN_MATH and math_masked[command.start] != masked[command.start]:
            group = read_group(masked, skip_ws(masked, command.end))
            if group is not None:
                spans.append((group[0], group[1]))
    return spans


def collect_settings(graph: SourceGraph) -> tuple[Settings, dict[str, str]]:
    """Preamble ``\\sisetup`` settings, ``\\DeclareSIUnit`` definitions and the babel language words."""
    main = graph.files.get(graph.main)
    settings = Settings()
    declared: dict[str, str] = {}
    if main is None:
        return settings, declared
    masked, text = main.masked, main.text
    begin = _BEGIN_DOCUMENT.search(masked)
    limit = begin.start() if begin else len(masked)
    language = babel_language(masked[:limit], text[:limit])
    if language in LANGUAGE_WORDS:
        settings = replace(settings, words=dict(LANGUAGE_WORDS[language]))
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        for command in iter_commands(scanned.masked):
            if command.name == "sisetup":
                group = read_group(scanned.masked, skip_ws(scanned.masked, command.end))
                if group is not None:
                    settings = apply_options(settings, scanned.text[group[0] : group[1]])
            elif command.name == "DeclareSIUnit":
                pos = skip_ws(scanned.masked, command.end)
                optional = read_optional(scanned.masked, pos)
                if optional is not None:
                    pos = skip_ws(scanned.masked, optional[2])
                name_group = read_group(scanned.masked, pos)
                if name_group is None:
                    continue
                name = scanned.text[name_group[0] : name_group[1]].strip().lstrip("\\")
                body = read_group(scanned.masked, skip_ws(scanned.masked, name_group[2]))
                if body is not None and name.isalpha():
                    declared[name] = scanned.text[body[0] : body[1]]
    return settings, declared


def plan_siunitx_rewrites(graph: SourceGraph) -> SiunitxPlan:
    """Edits that replace the siunitx commands of the document body by text or math."""
    plan = SiunitxPlan()
    settings, declared = collect_settings(graph)
    ctx = _Context(settings, UnitParser(declared))
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked, text = scanned.masked, scanned.text
        body_start = 0
        if rel == graph.main:
            begin = _BEGIN_DOCUMENT.search(masked)
            if begin is None:
                continue
            body_start = begin.end()
        math_masked = mask_math(masked)
        text_spans = _text_in_math_spans(masked, math_masked)
        for command in iter_commands(masked):
            name = command.name
            if name not in COMMAND_ARGS or command.start < body_start:
                continue
            pos = skip_ws(masked, command.end)
            options = None
            optional = read_optional(masked, pos)
            if optional is not None:
                options = text[optional[0] : optional[1]]
                pos = skip_ws(masked, optional[2])
            args: list[str] = []
            end = pos
            for _ in range(COMMAND_ARGS[name]):
                group = read_group(masked, skip_ws(masked, end))
                if group is None:
                    break
                args.append(text[group[0] : group[1]])
                end = group[2]
            where = SourceLocation(rel, scanned.lines.line(command.start))
            if len(args) != COMMAND_ARGS[name]:
                plan.findings.append(Finding("L2W-W-UNIT-001", Severity.WARNING,
                                             f"\\{name}: expected {COMMAND_ARGS[name]} braced argument(s); left unchanged", where))
                continue
            in_math = math_masked[command.start] != masked[command.start]
            if in_math and any(a <= command.start < b for a, b in text_spans):
                in_math = False
            try:
                replacement = expand_command(name, args, options, ctx, in_math)
            except UnitError as exc:
                plan.findings.append(Finding("L2W-W-UNIT-001", Severity.WARNING,
                                             f"\\{name}{{{'}{'.join(args)}}}: {exc}; left for Pandoc, which may render it incompletely",
                                             where))
                continue
            if not in_math:
                replacement = "{" + replacement + "}"  # keeps a following letter from joining a \textsuperscript
            plan.edits.append(Edit(rel, command.start, end, replacement, "siunitx",
                                   "siunitx command expanded (Pandoc drops declared units and does not convert it in math)",
                                   name))
            plan.expanded += 1
    return plan
