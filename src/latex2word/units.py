"""Physical lengths with one internal unit.

Every layout dimension is held as an integer number of EMU (English Metric
Units, 914400 per inch). All the profile units (``in``, ``cm``, ``mm``, ``pt``)
and the Word units (twip = 1/20 pt, EMU for drawings) are integer multiples of
one EMU, so conversions between them are exact and rounding happens only at the
boundary where Word needs an integer in a coarser unit (:meth:`Length.twips`).

Profiles write a length as ``"0.75in"`` or as ``{value: 0.75, unit: in}``;
:func:`parse_length` accepts both and rejects everything else.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

EMU_PER_INCH = 914_400
EMU_PER_UNIT: dict[str, int] = {"in": EMU_PER_INCH, "pt": 12_700, "cm": 360_000, "mm": 36_000}
EMU_PER_TWIP = 635
UNITS = tuple(EMU_PER_UNIT)
LENGTH_PATTERN = r"^[0-9]+(\.[0-9]+)?(in|pt|cm|mm)$"
_TEXT = re.compile(r"^(?P<value>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>in|pt|cm|mm)$")


class UnitError(ValueError):
    """A length could not be parsed."""


@dataclass(frozen=True, order=True)
class Length:
    """A non-negative physical length, stored as whole EMU."""

    emu: int

    def __post_init__(self) -> None:
        if isinstance(self.emu, bool) or not isinstance(self.emu, int):
            raise UnitError(f"a length is an integer number of EMU, got {self.emu!r}")
        if self.emu < 0:
            raise UnitError("a length cannot be negative")

    # -- construction ------------------------------------------------------------------------
    @classmethod
    def of(cls, value: float | int | str | Decimal, unit: str) -> Length:
        if unit not in EMU_PER_UNIT:
            raise UnitError(f"unknown unit {unit!r}; use one of {', '.join(UNITS)}")
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise UnitError(f"not a number: {value!r}") from exc
        if not number.is_finite() or number < 0:
            raise UnitError(f"a length must be a finite, non-negative number, got {value!r}")
        return cls(int((number * EMU_PER_UNIT[unit]).to_integral_value(ROUND_HALF_UP)))

    @classmethod
    def inches(cls, value: float | int | str | Decimal) -> Length:
        return cls.of(value, "in")

    @classmethod
    def points(cls, value: float | int | str | Decimal) -> Length:
        return cls.of(value, "pt")

    @classmethod
    def from_twips(cls, twips: int) -> Length:
        return cls(twips * EMU_PER_TWIP)

    # -- views -------------------------------------------------------------------------------
    @property
    def in_(self) -> float:
        return self.emu / EMU_PER_INCH

    @property
    def pt(self) -> float:
        return self.emu / EMU_PER_UNIT["pt"]

    @property
    def cm(self) -> float:
        return self.emu / EMU_PER_UNIT["cm"]

    @property
    def mm(self) -> float:
        return self.emu / EMU_PER_UNIT["mm"]

    @property
    def twips(self) -> int:
        """Nearest whole twip (half up): the only place layout values are rounded for Word."""
        return int((Decimal(self.emu) / EMU_PER_TWIP).to_integral_value(ROUND_HALF_UP))

    # -- arithmetic (kept exact in EMU) --------------------------------------------------------
    def __add__(self, other: Length) -> Length:
        return Length(self.emu + other.emu)

    def __sub__(self, other: Length) -> Length:
        return Length(self.emu - other.emu)

    def scaled(self, factor: float | Decimal) -> Length:
        """This length times ``factor`` (rounded to the nearest EMU)."""
        value = (Decimal(self.emu) * Decimal(str(factor))).to_integral_value(ROUND_HALF_UP)
        return Length(int(value))

    def divided(self, parts: int) -> Length:
        if parts < 1:
            raise UnitError("cannot divide a length into fewer than one part")
        return Length(int((Decimal(self.emu) / parts).to_integral_value(ROUND_HALF_UP)))

    def __str__(self) -> str:
        return f"{self.in_:.4f}in"


ZERO = Length(0)


def parse_length(value: Any) -> Length:
    """Parse ``"0.75in"``, ``{value: 0.75, unit: in}`` or an existing :class:`Length`."""
    if isinstance(value, Length):
        return value
    if isinstance(value, str):
        match = _TEXT.match(value.strip())
        if not match:
            raise UnitError(f"invalid length {value!r}: write a number and a unit, e.g. 0.75in (units: {', '.join(UNITS)})")
        return Length.of(match.group("value"), match.group("unit"))
    if isinstance(value, Mapping):
        if set(value) != {"value", "unit"}:
            raise UnitError(f"invalid length {dict(value)!r}: expected exactly the keys 'value' and 'unit'")
        raw = value["value"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise UnitError(f"invalid length value {raw!r}: expected a number")
        return Length.of(repr(raw) if isinstance(raw, float) else raw, str(value["unit"]))
    raise UnitError(f"invalid length {value!r}: expected a string such as 0.75in or a mapping with value and unit")
