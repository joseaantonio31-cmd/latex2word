"""Physical lengths: exact EMU arithmetic, parsing, and rounding only at the Word twip boundary."""

from __future__ import annotations

import pytest

from latex2word.units import EMU_PER_INCH, Length, UnitError, parse_length


@pytest.mark.parametrize(
    ("text", "twips"),
    [("0.75in", 1080), ("1in", 1440), ("0.625in", 900), ("0.25in", 360), ("2.54cm", 1440), ("25.4mm", 1440), ("72pt", 1440), ("0.5pt", 10)],
)
def test_every_profile_unit_converts_exactly_to_twips(text: str, twips: int):
    assert parse_length(text).twips == twips


def test_all_units_agree_on_one_physical_length():
    assert parse_length("1in") == parse_length("72pt") == parse_length("2.54cm") == parse_length("25.4mm")
    assert Length.inches("1").emu == EMU_PER_INCH


def test_structured_form_equals_the_compact_form():
    assert parse_length({"value": 0.75, "unit": "in"}) == parse_length("0.75in")
    assert parse_length({"value": 3, "unit": "mm"}) == parse_length("3mm")


def test_letter_and_a4_page_sizes_in_twips():
    assert (Length.inches("8.5").twips, Length.inches("11").twips) == (12240, 15840)
    assert (Length.of("210", "mm").twips, Length.of("297", "mm").twips) == (11906, 16838)  # Word's own A4 values


def test_rounding_happens_only_at_the_twip_boundary():
    third = Length.inches("1").divided(3)
    assert (third + third + third).emu in range(EMU_PER_INCH - 2, EMU_PER_INCH + 3)
    assert Length(3175).twips == 5  # 5 twips = 3175 EMU exactly
    assert Length(3175 + 317).twips == 5 and Length(3175 + 318).twips == 6  # half up at 0.5 twip = 317.5 EMU


def test_scaling_keeps_the_exact_ratio():
    assert Length.inches("3.5").scaled(0.98).in_ == pytest.approx(3.43)


@pytest.mark.parametrize("bad", ["1", "in", "1 furlong", "-1in", "1e3in", "0,5in", "", " ", None, True, 3, {"value": 1}, {"value": "1", "unit": "in"}, {"value": 1, "unit": "em"}, {"value": -1, "unit": "in"}, {"value": 1, "unit": "in", "x": 1}])
def test_invalid_lengths_are_rejected(bad):
    with pytest.raises(UnitError):
        parse_length(bad)


def test_a_length_cannot_be_negative():
    with pytest.raises(UnitError):
        Length(-1)
    with pytest.raises(UnitError):
        Length.of(-0.5, "in")
