"""TeX text fragments become exact Unicode; anything unconvertible is reported, never dropped."""

from __future__ import annotations

import pytest

from latex2word.metadata.tex_text import tex_to_text


def one(source: str) -> str:
    result = tex_to_text(source)
    assert result.problems == ()
    assert len(result.lines) == 1
    return result.lines[0]


def test_raw_unicode_passes_through_exactly():
    text = "Zoë Örnek Nuño Ibáñez Łukasz Ṧ 李雷 e\u0301"
    assert one(text) == text


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r"\'e", "é"), (r"\'{e}", "é"), (r'\"o', "ö"), (r"\~n", "ñ"), (r"\^o", "ô"), (r"\`a", "à"),
        (r"\c{c}", "ç"), (r"\c c", "ç"), (r"\u{g}", "ğ"), (r"\v{c}", "č"), (r"\H{o}", "ő"), (r"\r{a}", "å"),
        (r"\=a", "ā"), (r"\.z", "ż"), (r"\k{a}", "ą"), (r"\'{\i}", "í"), (r"Jos\'e", "José"),
    ],
)
def test_accents_compose_to_nfc(source, expected):
    assert one(source) == expected


def test_special_letters_and_ligatures():
    assert one(r"\i \j \ss \ae \AE \oe \o \O \l \L \aa \AA") == "ıȷßæÆœøØłŁåÅ"  # TeX eats the blank after a control word
    assert one(r"{\ss} {\o}") == "ß ø"
    assert one(r"Stra\ss e") == "Straße"


def test_escapes_nbsp_dashes_and_quotes():
    assert one(r"A \& B \% C \_ D \# E \$ F \{ G \}") == "A & B % C _ D # E $ F { G }"
    assert one("Dr.~Smith") == "Dr.\u00a0Smith"
    assert one("a--b---c") == "a\u2013b\u2014c"
    assert one("``quoted''") == "\u201cquoted\u201d"


def test_groups_and_text_wrappers_keep_their_text():
    assert one(r"{Ada} \textit{Example} \emph{A} \textbf{B} \textsc{C} \textrm{D} \textsf{E} \texttt{F} \textnormal{G} \mbox{H} \text{I}") == "Ada Example A B C D E F G H I"
    assert one(r"\makebox[3cm][c]{Centered}") == "Centered"
    assert one(r"\texttt{ada@example.org}") == "ada@example.org"
    assert one(r"\href{mailto:ada@example.org}{ada@example.org}") == "ada@example.org"


def test_layout_switches_carry_no_text():
    source = (
        r"{\normalsize\small\footnotesize\scriptsize\large\Large\itshape\bfseries\mdseries\upshape\scshape\rmfamily"
        r"\sffamily\ttfamily\centering\raggedright\raggedleft\selectfont Ada}"
        r"\noindent\strut\protect\relax\ignorespaces\par\fontsize{8.5}{9.5}\vspace{2pt}\vspace*{2pt}\hspace{1em}\hspace*{1em}\vskip 3pt plus 1fil\hskip 2pt"
    )
    assert one(source) == "Ada"


def test_spacing_commands_separate_words():
    assert one(r"Ada\hfill Example") == "Ada Example"
    assert one(r"Ada\quad Example\qquad X") == "Ada Example X"


def test_line_breaks_split_lines_and_empty_lines_are_dropped():
    result = tex_to_text("Name \\\\[2pt] Dept \\newline Univ\\linebreak City \\\\ \\\\  \n  Country\n")
    assert result.lines == ("Name", "Dept", "Univ", "City", "Country")
    assert result.problems == ()


def test_whitespace_is_collapsed_but_nbsp_is_kept():
    assert one("  a \t\n  b~~c  ") == "a b\u00a0\u00a0c"


def test_comments_are_not_text():
    assert one("Ada % hidden\nExample") == "Ada Example"


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        (r"Ada\thanks{Funded by X}", "\\thanks"),
        (r"Ada\footnote{note}", "\\footnote"),
        (r"Ada \unknowncmd{text}", "\\unknowncmd"),
        (r"Ada \alpha", "\\alpha"),
        ("Ada $x^2$", "math"),
        ("Ada $$x$$", "math"),
        (r"Ada {unclosed", "unbalanced"),
        (r"Ada } stray", "unbalanced"),
        (r"Ada \'{}", "accent"),
    ],
)
def test_content_bearing_constructs_are_reported_not_dropped(source, needle):
    result = tex_to_text(source)
    assert any(needle in problem for problem in result.problems), result.problems
    assert result.lines  # the readable part is still produced


def test_a_problem_does_not_stop_the_conversion():
    result = tex_to_text(r"Zoë\thanks{x} Örnek")
    assert result.lines == ("Zoë Örnek",)
    assert len(result.problems) == 1
