"""Author blocks written with the ``IEEEauthorblock`` macros or with ``\\parbox`` columns.

Selected by profile configuration only (``metadata_adapter: ieee-author-blocks``), never by the
document class. It behaves like the generic adapter (``\\title``, ``\\date``, keywords, removal of
the ``\\author`` command from the staged copy) and only replaces how the ``\\author`` argument is
read. Two declaration styles are understood:

* ``\\IEEEauthorblockN{Name}`` starts an author; an optional ``\\IEEEauthorblockA{line\\\\line\\\\email}``
  attaches affiliation lines and addresses to the preceding author. Blocks may be separated by
  ``\\and`` or simply concatenated.
* Column layouts: every ``\\parbox[t]{<width>}{...}`` cell is one author, optionally wrapped in
  ``\\makebox[..][..]{...}`` and separated by ``\\hfill``, comments and whitespace. Inside a cell
  the lines (split on ``\\\\``) are: first line = name, a line containing ``@`` = e-mail address,
  all other lines = affiliation. Font and size switches such as ``{\\normalsize Name}`` or
  ``{\\fontsize{8.5}{9.5}\\selectfont\\itshape ...}`` carry no text and are ignored.

An ``\\author`` that uses none of these constructs is read exactly like the generic adapter reads it.
Any other text in an ``\\author`` that does use them (``\\IEEEauthorrefmark``, footnotes, stray words) is reported
as an error rather than dropped: the number of authors always equals the number of blocks/cells.
"""

from __future__ import annotations

import re

from latex2word.latex_scan import read_group, read_optional, skip_ws
from latex2word.metadata.adapters import register_adapter
from latex2word.metadata.generic import AuthorParse, GenericAdapter, ParsedAuthor
from latex2word.metadata.tex_text import tex_to_text

_WORD = re.compile(r"[A-Za-z]+")
_BLOCK_COMMAND = re.compile(r"\\(?:IEEEauthorblock[NA]|parbox)(?![A-Za-z])")


class IeeeAuthorBlocksAdapter(GenericAdapter):
    name = "ieee-author-blocks"
    extra_consumed_commands = frozenset({"IEEEauthorblockN", "IEEEauthorblockA", "parbox", "makebox", "hfill"})

    def parse_authors(self, argument: str) -> AuthorParse:
        if not _BLOCK_COMMAND.search(argument):
            return super().parse_authors(argument)  # plain ``Name \\ line \\ email \and ...`` declarations
        result = AuthorParse()
        residual: list[str] = []
        _walk(argument, result, residual)
        leftover = tex_to_text("".join(residual))
        result.problems.extend(leftover.problems)
        if leftover.lines:
            result.problems.append(f"text outside the author blocks: {' / '.join(leftover.lines)}")
        return result


def _walk(text: str, result: AuthorParse, residual: list[str]) -> None:
    """Consume known constructs into ``result``; everything else is appended to ``residual`` for checking."""
    i, n = 0, len(text)
    while i < n:
        if text[i] != "\\":
            residual.append(text[i])
            i += 1
            continue
        word = _WORD.match(text, i + 1)
        if word is None:
            residual.append(text[i : i + 2])
            i += 2
            continue
        handler = _HANDLERS.get(word.group(0))
        if handler is None:
            residual.append(text[i : word.end()])
            i = word.end()
            continue
        i = handler(text, word.end(), result, residual)


def _separator(text: str, after: int, result: AuthorParse, residual: list[str]) -> int:
    residual.append(" ")
    return after


def _lines_of(text: str, group: tuple[int, int, int], result: AuthorParse) -> tuple[str, ...]:
    converted = tex_to_text(text[group[0] : group[1]])
    result.problems.extend(converted.problems)
    return converted.lines


def _block_name(text: str, after: int, result: AuthorParse, residual: list[str]) -> int:
    group = read_group(text, skip_ws(text, after))
    if group is None:
        result.problems.append("\\IEEEauthorblockN without its argument")
        return after
    result.authors.append(ParsedAuthor.from_lines(_lines_of(text, group, result)))
    residual.append(" ")
    return group[2]


def _block_affiliation(text: str, after: int, result: AuthorParse, residual: list[str]) -> int:
    group = read_group(text, skip_ws(text, after))
    if group is None:
        result.problems.append("\\IEEEauthorblockA without its argument")
        return after
    lines = _lines_of(text, group, result)
    if result.authors:
        result.authors[-1].add_lines(lines)
    else:
        result.problems.append("\\IEEEauthorblockA without a preceding \\IEEEauthorblockN")
    residual.append(" ")
    return group[2]


def _skip_optionals(text: str, pos: int, count: int) -> int:
    pos = skip_ws(text, pos)
    for _ in range(count):
        optional = read_optional(text, pos)
        if optional is None:
            break
        pos = skip_ws(text, optional[2])
    return pos


def _parbox(text: str, after: int, result: AuthorParse, residual: list[str]) -> int:
    """``\\parbox[pos][height][inner]{width}{cell}``: one author per cell."""
    pos = _skip_optionals(text, after, 3)
    width = read_group(text, pos)
    cell = read_group(text, skip_ws(text, width[2])) if width else None
    if cell is None:
        result.problems.append("\\parbox without its width and content arguments")
        return after
    author = ParsedAuthor.from_lines(_lines_of(text, cell, result))
    if not author.empty:
        result.authors.append(author)
    residual.append(" ")
    return cell[2]


def _makebox(text: str, after: int, result: AuthorParse, residual: list[str]) -> int:
    """A wrapper only: its content is read like the surrounding text."""
    group = read_group(text, _skip_optionals(text, after, 2))
    if group is None:
        result.problems.append("\\makebox without a braced argument")
        return after
    _walk(text[group[0] : group[1]], result, residual)
    return group[2]


_HANDLERS = {
    "and": _separator,
    "hfill": _separator,
    "IEEEauthorblockN": _block_name,
    "IEEEauthorblockA": _block_affiliation,
    "parbox": _parbox,
    "makebox": _makebox,
}

register_adapter(IeeeAuthorBlocksAdapter())
