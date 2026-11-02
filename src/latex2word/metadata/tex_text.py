"""Convert a small TeX text fragment (author names, affiliation lines) to exact Unicode.

Front-matter text must reach the document character for character: raw Unicode is passed
through untouched (never normalised or stripped of diacritics), TeX accents are composed to
NFC, and the few commands that only select a font or add spacing are dropped because they
carry no text. Anything else that could carry content (an unknown command, math, a
footnote) is never dropped silently: it is reported in :attr:`TexText.problems` so the
caller can fail closed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from latex2word.latex_scan import read_group, read_optional, skip_ws

_BREAK = "\n"  # internal marker; real source newlines are whitespace
_SPACE_RUN = re.compile(r"[ \t\r\f\v]+")
_WORD = re.compile(r"[A-Za-z]+")
_DIMENSION = re.compile(
    r"\s*[-+]?\s*(?:\d+\.?\d*|\.\d+)\s*(?:pt|bp|cm|mm|in|em|ex|pc|sp|dd|cc|mu)"
    r"(?:\s*plus\s*[-+]?[\d.]+\s*[A-Za-z]+)?(?:\s*minus\s*[-+]?[\d.]+\s*[A-Za-z]+)?"
)

# Accent control symbols (``\'e``) and control words (``\c{c}``) -> combining mark.
_SYMBOL_ACCENTS = {"'": "\u0301", '"': "\u0308", "~": "\u0303", "^": "\u0302", "`": "\u0300", "=": "\u0304", ".": "\u0307"}
_WORD_ACCENTS = {
    "c": "\u0327", "u": "\u0306", "v": "\u030c", "H": "\u030b", "r": "\u030a", "k": "\u0328", "b": "\u0331", "d": "\u0323",
}
_LETTERS = {
    "i": "\u0131", "j": "\u0237", "ss": "\u00df", "ae": "\u00e6", "AE": "\u00c6", "oe": "\u0153", "OE": "\u0152",
    "o": "\u00f8", "O": "\u00d8", "l": "\u0142", "L": "\u0141", "aa": "\u00e5", "AA": "\u00c5",
}
_ESCAPES = {"&": "&", "%": "%", "_": "_", "#": "#", "$": "$", "{": "{", "}": "}", " ": " ", ",": " ", ";": " ", ":": " ", "!": "", "/": "", "-": ""}
# Commands that keep the text of their (last) braced argument.
_WRAPPERS = frozenset(
    {"textit", "emph", "textbf", "textsc", "textrm", "textsf", "texttt", "textnormal", "mbox", "text", "email", "url"}
)
# Switches that carry no text; dropped.
_SWITCHES = frozenset(
    {
        "normalsize", "small", "footnotesize", "scriptsize", "large", "Large", "itshape", "bfseries", "mdseries", "upshape",
        "scshape", "rmfamily", "sffamily", "ttfamily", "centering", "raggedright", "raggedleft", "selectfont", "par",
        "noindent", "strut", "protect", "relax", "ignorespaces",
    }
)
_SPACING = frozenset({"hfill", "quad", "qquad"})
_BREAKS = frozenset({"newline", "linebreak"})
_ARGUMENT_SWITCHES = {"fontsize": 2, "vspace": 1, "hspace": 1}
_DIMENSION_SWITCHES = frozenset({"vskip", "hskip"})


@dataclass(frozen=True)
class TexText:
    lines: tuple[str, ...]
    problems: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """All lines joined by a single space."""
        return " ".join(self.lines)


def tex_to_text(source: str) -> TexText:
    """Convert ``source`` to Unicode lines (split on TeX line breaks) plus the constructs it could not convert."""
    problems: list[str] = []
    if not _balanced(source):
        problems.append("unbalanced braces")
    converted = _convert(source, problems)
    lines = (_SPACE_RUN.sub(" ", part).strip() for part in converted.split(_BREAK))
    return TexText(tuple(line for line in lines if line), tuple(problems))


def _balanced(source: str) -> bool:
    depth = 0
    i = 0
    while i < len(source):
        ch = source[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "%":
            i = source.find("\n", i)
            if i == -1:
                break
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


def _convert(s: str, problems: list[str]) -> str:
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            i = _command(s, i, out, problems)
        elif ch == "%":
            end = s.find("\n", i)
            i = n if end == -1 else end
        elif ch == "$":
            i = _math(s, i, problems)
        elif ch in "{}":
            i += 1
        elif ch == "~":
            out.append("\u00a0")
            i += 1
        elif s.startswith("---", i):
            out.append("\u2014")
            i += 3
        elif s.startswith("--", i):
            out.append("\u2013")
            i += 2
        elif s.startswith("``", i):
            out.append("\u201c")
            i += 2
        elif s.startswith("''", i):
            out.append("\u201d")
            i += 2
        elif ch == "\n":
            out.append(" ")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _math(s: str, i: int, problems: list[str]) -> int:
    delimiter = "$$" if s.startswith("$$", i) else "$"
    end = s.find(delimiter, i + len(delimiter))
    stop = len(s) if end == -1 else end + len(delimiter)
    problems.append(f"math is not supported: {_clip(s[i:stop])}")
    return stop


def _clip(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _command(s: str, i: int, out: list[str], problems: list[str]) -> int:
    """Handle the control sequence at ``s[i]``; return the offset after it and its arguments."""
    if i + 1 >= len(s):
        problems.append("a lone backslash at the end of the text")
        return i + 1
    nxt = s[i + 1]
    if nxt == "\\":
        optional = read_optional(s, i + 2)
        out.append(_BREAK)
        return optional[2] if optional else i + 2 + (1 if s.startswith("*", i + 2) else 0)
    word = _WORD.match(s, i + 1)
    if word is None:
        return _symbol_command(s, i, out, problems)
    name = word.group(0)
    after = word.end()
    if name in _WORD_ACCENTS:
        return _accent(s, skip_ws(s, after), _WORD_ACCENTS[name], out, problems)
    if name in _LETTERS:
        out.append(_LETTERS[name])
        return skip_ws(s, after)
    return _named_command(s, name, after, out, problems)


def _symbol_command(s: str, i: int, out: list[str], problems: list[str]) -> int:
    symbol = s[i + 1]
    if symbol in _SYMBOL_ACCENTS:
        return _accent(s, i + 2, _SYMBOL_ACCENTS[symbol], out, problems)
    if symbol in _ESCAPES:
        out.append(_ESCAPES[symbol])
        return i + 2
    problems.append(f"unsupported control symbol \\{symbol}")
    return i + 2


def _accent(s: str, pos: int, mark: str, out: list[str], problems: list[str]) -> int:
    """Compose the combining ``mark`` with the base letter that starts at ``pos``."""
    group = read_group(s, pos)
    if group is not None:
        raw, after = s[group[0] : group[1]].strip(), group[2]
    else:
        word = _WORD.match(s, pos + 1) if s[pos : pos + 1] == "\\" else None
        stop = word.end() if word else pos + 1
        raw, after = s[pos:stop], stop
    base = {"\\i": "i", "\\j": "j"}.get(raw)
    if base is None:
        base = _convert(raw, problems).strip()
    if not base:
        problems.append("an accent without a letter to apply to")
        return after
    out.append(unicodedata.normalize("NFC", base[0] + mark + base[1:]))
    return after


def _named_command(s: str, name: str, after: int, out: list[str], problems: list[str]) -> int:
    if name in _SWITCHES:
        return skip_ws(s, after)
    if name in _SPACING:
        out.append(" ")
        return skip_ws(s, after)
    if name in _BREAKS:
        out.append(_BREAK)
        optional = read_optional(s, skip_ws(s, after))
        return optional[2] if optional else skip_ws(s, after)
    if name in _DIMENSION_SWITCHES:
        match = _DIMENSION.match(s, after)
        return match.end() if match else skip_ws(s, after)
    starred = s.startswith("*", after)
    if name in _ARGUMENT_SWITCHES:
        return _skip_groups(s, after + (1 if starred else 0), _ARGUMENT_SWITCHES[name], problems, name)
    if name == "makebox":
        return _wrapper(s, after, out, problems, name, optionals=2)
    if name in _WRAPPERS:
        return _wrapper(s, after, out, problems, name)
    if name == "href":
        return _href(s, after, out, problems)
    return _unsupported(s, name, after, problems)


def _skip_groups(s: str, pos: int, count: int, problems: list[str], name: str) -> int:
    for _ in range(count):
        group = read_group(s, skip_ws(s, pos))
        if group is None:
            problems.append(f"\\{name} without its argument")
            return pos
        pos = group[2]
    return pos


def _wrapper(s: str, after: int, out: list[str], problems: list[str], name: str, optionals: int = 0) -> int:
    pos = skip_ws(s, after)
    for _ in range(optionals):
        optional = read_optional(s, pos)
        if optional is None:
            break
        pos = skip_ws(s, optional[2])
    group = read_group(s, pos)
    if group is None:
        problems.append(f"\\{name} without a braced argument")
        return pos
    out.append(_convert(s[group[0] : group[1]], problems))
    return group[2]


def _href(s: str, after: int, out: list[str], problems: list[str]) -> int:
    """``\\href{target}{text}`` keeps its text; the link target is not display content."""
    target = read_group(s, skip_ws(s, after))
    text = read_group(s, skip_ws(s, target[2])) if target else None
    if text is None:
        problems.append("\\href without its two arguments")
        return target[2] if target else after
    out.append(_convert(s[text[0] : text[1]], problems))
    return text[2]


def _unsupported(s: str, name: str, after: int, problems: list[str]) -> int:
    """Report a command that may carry content and skip its arguments so the scan can go on."""
    end = skip_ws(s, after)  # TeX eats the blank after a control word, not after an argument
    shown = [f"\\{name}"]
    while True:
        pos = skip_ws(s, end)
        group = read_optional(s, pos) or read_group(s, pos)
        if group is None:
            break
        shown.append(_clip(s[pos : group[2]]))
        end = group[2]
    problems.append(f"unsupported command {''.join(shown)}")
    return end
