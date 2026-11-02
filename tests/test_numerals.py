from __future__ import annotations

import pytest

from latex2word.numerals import (
    NumeralError,
    format_numeral,
    parse_numeral,
    render_template,
    template_problems,
    template_regex,
)


@pytest.mark.parametrize(
    ("n", "style", "text"),
    [
        (1, "arabic", "1"), (12, "arabic", "12"),
        (1, "Roman", "I"), (4, "Roman", "IV"), (9, "Roman", "IX"), (14, "Roman", "XIV"), (1994, "Roman", "MCMXCIV"),
        (3999, "roman", "mmmcmxcix"),
        (1, "Alpha", "A"), (26, "Alpha", "Z"), (3, "alpha", "c"),
    ],
)
def test_numerals_round_trip(n: int, style: str, text: str):
    assert format_numeral(n, style) == text
    assert parse_numeral(text, style) == n


@pytest.mark.parametrize(("n", "style"), [(0, "arabic"), (-1, "Roman"), (4000, "Roman"), (27, "Alpha"), (1, "bogus")])
def test_numbers_that_cannot_be_written_are_rejected(n: int, style: str):
    with pytest.raises(NumeralError):
        format_numeral(n, style)


@pytest.mark.parametrize(("text", "style"), [("IIII", "Roman"), ("iv", "Roman"), ("07", "arabic"), ("AA", "Alpha"), ("", "arabic")])
def test_non_canonical_text_is_not_a_numeral(text: str, style: str):
    assert parse_numeral(text, style) is None


def test_template_regex_reads_numbers_back_from_rendered_text():
    regex = template_regex("TABLE {n}", {"n": "Roman"})
    assert parse_numeral(regex.fullmatch("TABLE IV")["n"], "Roman") == 4
    assert regex.fullmatch("Table IV") is None
    forced = template_regex("Fig. {n}/{Roman}", {"n": "arabic"}).fullmatch("Fig. 3/III")
    assert forced["n"] == "3" and forced["Roman"] == "III"


def test_template_regex_matches_whitespace_runs_and_hierarchical_fields():
    match = template_regex("Section {n1}-{n}", {"n": "Alpha", "n1": "Roman"}).fullmatch("Section\u00a0II-B")
    assert (match["n1"], match["n"]) == ("II", "B")


def test_template_problems_name_unknown_fields_and_bad_braces():
    assert template_problems("Fig. {n}", ["n"]) == []
    assert "unknown field {x}" in template_problems("{x}", ["n"])[0]
    assert template_problems("{n", ["n"]) == ["unbalanced or nested braces"]


def test_render_template_substitutes_every_field():
    assert render_template("({n})", {"n": "7"}) == "(7)"
