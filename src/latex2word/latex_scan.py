"""Conservative LaTeX structural scanner.

This is *not* a TeX interpreter. It provides just enough lexical machinery to
locate a handful of structural commands reliably:

* :func:`mask_non_live` blanks out everything that is not live source
  (comments, verbatim-like environments, ``\\verb``, ``\\iffalse ... \\fi``)
  while preserving every offset and newline, so line numbers stay exact;
* :func:`iter_commands` tokenises control sequences;
* :func:`read_group` / :func:`read_optional` read balanced ``{...}`` / ``[...]``
  arguments;
* :func:`scan_environments` finds top-level environments with nesting and
  mismatch awareness.

All functions operate on *masked* text unless stated otherwise.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

__all__ = [
    "Command",
    "Environment",
    "LineIndex",
    "MaskResult",
    "ScanProblem",
    "iter_commands",
    "mask_non_live",
    "read_group",
    "read_optional",
    "scan_environments",
    "skip_ws",
    "split_top_level",
]

# Environments whose body is never live LaTeX source.
_VERBATIM_ENVIRONMENTS = frozenset(
    {"verbatim", "verbatim*", "Verbatim", "Verbatim*", "lstlisting", "minted", "comment", "filecontents", "filecontents*"}
)
# TeX-looking conditionals that are LaTeX/etoolbox macros and need no matching \fi.
_NOT_TEX_CONDITIONALS = frozenset(
    {"ifthenelse", "iftoggle", "ifdef", "ifundef", "ifstrequal", "ifbool", "ifblank", "ifnumcomp", "ifdimcomp", "ifcsdef", "ifcsundef"}
)
_WORD = re.compile(r"[A-Za-z]+")
_LINE_COMMENT = re.compile(r"(?<!\\)%[^\n]*")
_NON_NEWLINE = re.compile(r"[^\r\n]")
_WS = " \t\r\n\f"


@dataclass(frozen=True)
class ScanProblem:
    """A lexical problem found while scanning (offset into the scanned text)."""

    offset: int
    message: str


@dataclass(frozen=True)
class MaskResult:
    text: str
    problems: tuple[ScanProblem, ...]


@dataclass(frozen=True)
class Command:
    """A control sequence: ``start`` is the backslash, ``end`` is just after the name (and ``*``)."""

    name: str
    start: int
    end: int
    starred: bool = False


class LineIndex:
    """Offset -> 1-based line number for a text."""

    def __init__(self, text: str) -> None:
        self._newlines = [i for i, ch in enumerate(text) if ch == "\n"]

    def line(self, offset: int) -> int:
        return bisect.bisect_left(self._newlines, offset) + 1


def _blank(segment: str) -> str:
    return _NON_NEWLINE.sub(" ", segment)


def _control_word_at(text: str, backslash: int) -> str | None:
    match = _WORD.match(text, backslash + 1)
    return match.group(0) if match else None


def _end_of_environment(text: str, start: int, name: str) -> int | None:
    """Offset after ``\\end{name}`` for the environment whose body starts at ``start``."""
    pattern = re.compile(r"\\end\s*\{" + re.escape(name) + r"\}")
    match = pattern.search(text, start)
    return match.end() if match else None


def _end_of_iffalse(text: str, start: int) -> int | None:
    """Offset after the ``\\fi`` closing a conditional whose body starts at ``start``."""
    view = _LINE_COMMENT.sub(lambda m: _blank(m.group()), text[start:])
    depth = 1
    skip_next = False
    for command in iter_commands(view):
        name = command.name
        if skip_next:  # the \iffoo declared by \newif\iffoo is not a conditional
            skip_next = False
            continue
        if name == "newif":
            skip_next = True
        elif name == "fi":
            depth -= 1
            if depth == 0:
                return start + command.end
        elif name.startswith("if") and name not in _NOT_TEX_CONDITIONALS:
            depth += 1
    return None


def mask_non_live(text: str) -> MaskResult:
    """Blank out non-live regions, preserving length and newlines.

    Non-live: ``%`` comments, ``\\verb`` arguments, verbatim-like environments
    (including the ``comment`` package) and ``\\iffalse ... \\fi`` bodies.
    Unterminated verbatim-like/``\\iffalse`` regions are masked to the end of the
    text and reported as problems.
    """
    out: list[str] = []
    problems: list[ScanProblem] = []
    n = len(text)
    i = 0
    last = 0  # start of the not-yet-copied live segment

    def mask(start: int, stop: int) -> None:
        nonlocal last
        out.append(text[last:start])
        out.append(_blank(text[start:stop]))
        last = stop

    while i < n:
        ch = text[i]
        if ch == "%":
            end = text.find("\n", i)
            end = n if end == -1 else end
            mask(i, end)
            i = end
            continue
        if ch != "\\":
            i += 1
            continue
        word = _control_word_at(text, i)
        if word is None:  # control symbol such as \\, \%, \{ : skip both characters
            i += 2
            continue
        after = i + 1 + len(word)
        if word == "verb":
            j = after + 1 if after < n and text[after] == "*" else after
            if j < n and text[j] not in _WS:
                delimiter = text[j]
                close = text.find(delimiter, j + 1)
                eol = text.find("\n", j + 1)
                if close != -1 and (eol == -1 or close < eol):
                    mask(i, close + 1)
                    i = close + 1
                    continue
            i = after
            continue
        if word == "begin":
            match = re.compile(r"\s*\{([^{}]*)\}").match(text, after)
            if match and match.group(1).strip() in _VERBATIM_ENVIRONMENTS:
                name = match.group(1).strip()
                end = _end_of_environment(text, match.end(), name)
                if end is None:
                    problems.append(ScanProblem(i, f"unterminated environment {name!r}; treated as non-live to end of file"))
                    end = n
                mask(i, end)
                i = end
                continue
            i = after
            continue
        if word == "iffalse":
            end = _end_of_iffalse(text, after)
            if end is None:
                problems.append(ScanProblem(i, "unterminated \\iffalse; treated as non-live to end of file"))
                end = n
            mask(i, end)
            i = end
            continue
        i = after
    out.append(text[last:])
    return MaskResult("".join(out), tuple(problems))


def iter_commands(text: str, start: int = 0) -> Iterator[Command]:
    """Yield control sequences of (masked) ``text`` in order, from offset ``start``."""
    n = len(text)
    i = text.find("\\", start)
    while i != -1 and i < n:
        if i + 1 >= n:
            return
        word = _control_word_at(text, i)
        if word is None:
            yield Command(text[i + 1], i, i + 2)
            i = text.find("\\", i + 2)
            continue
        end = i + 1 + len(word)
        starred = end < n and text[end] == "*"
        if starred:
            end += 1
        yield Command(word, i, end, starred)
        i = text.find("\\", end)


def skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos] in _WS:
        pos += 1
    return pos


def _read_balanced(text: str, pos: int, opener: str, closer: str) -> tuple[int, int, int] | None:
    """Read a balanced ``opener ... closer`` group at ``pos``.

    Returns ``(content_start, content_end, after)`` or ``None`` when ``text[pos]``
    is not ``opener`` or the group is unbalanced. Backslash escapes are skipped.
    For ``[...]`` groups, brackets inside ``{...}`` do not count.
    """
    n = len(text)
    if pos >= n or text[pos] != opener:
        return None
    depth = 0
    brace_depth = 0
    i = pos
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if opener == "{":
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return pos + 1, i, i + 1
        else:
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth = max(0, brace_depth - 1)
            elif brace_depth == 0:
                if ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        return pos + 1, i, i + 1
        i += 1
    return None


def read_group(text: str, pos: int) -> tuple[int, int, int] | None:
    """Read a balanced ``{...}`` at ``pos`` -> ``(content_start, content_end, after)``."""
    return _read_balanced(text, pos, "{", "}")


def read_optional(text: str, pos: int) -> tuple[int, int, int] | None:
    """Read a balanced ``[...]`` at ``pos`` -> ``(content_start, content_end, after)``."""
    return _read_balanced(text, pos, "[", "]")


def split_top_level(text: str, separator: str = ",") -> list[str]:
    """Split on ``separator`` outside ``{...}``; pieces are stripped, empties dropped."""
    pieces: list[str] = []
    depth = 0
    current: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            current.append(text[i : i + 2])
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == separator and depth == 0:
            pieces.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    pieces.append("".join(current))
    return [p.strip() for p in pieces if p.strip()]


@dataclass(frozen=True)
class Environment:
    """A top-level ``\\begin{name} ... \\end{name}`` span (offsets into the scanned text)."""

    name: str
    begin: int  # offset of the backslash of \begin
    content_start: int  # just after \begin{name}
    content_end: int  # offset of the backslash of \end
    end: int  # just after \end{name}


def _environment_name(text: str, command: Command) -> tuple[str, int] | None:
    pos = skip_ws(text, command.end)
    group = read_group(text, pos)
    if group is None:
        return None
    start, stop, after = group
    return text[start:stop].strip(), after


def scan_environments(
    text: str, names: Sequence[str]
) -> tuple[list[Environment], list[ScanProblem]]:
    """Find top-level environments named in ``names`` in masked ``text``.

    Nesting is tracked with a stack, so ``\\end`` of an inner environment never
    terminates the outer one. Only outermost matches are returned. Environments
    not listed in ``names`` are ignored. Stray ``\\end``, mismatched pairs and
    unclosed environments are reported as problems (the affected environment is
    not returned); scanning continues past a stray/mismatched ``\\end``.
    """
    wanted = set(names)
    found: list[Environment] = []
    problems: list[ScanProblem] = []
    stack: list[tuple[str, int, int]] = []  # (name, begin, content_start)
    for command in iter_commands(text):
        if command.name not in ("begin", "end"):
            continue
        parsed = _environment_name(text, command)
        if parsed is None:
            continue
        name, after = parsed
        if name not in wanted:
            continue
        if command.name == "begin":
            stack.append((name, command.start, after))
            continue
        if not stack:
            problems.append(ScanProblem(command.start, f"\\end{{{name}}} without matching \\begin"))
            continue
        open_name, begin, content_start = stack[-1]
        if open_name != name:
            problems.append(
                ScanProblem(command.start, f"\\end{{{name}}} does not match \\begin{{{open_name}}}")
            )
            continue
        stack.pop()
        if not stack:
            found.append(Environment(name, begin, content_start, command.start, after))
    for name, begin, _ in stack[:1]:
        problems.append(ScanProblem(begin, f"unclosed \\begin{{{name}}}"))
    return found, problems
