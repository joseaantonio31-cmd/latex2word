"""Publisher-neutral metadata extraction from ``\\title``, ``\\author``, ``\\date`` and keyword markup.

LaTeX semantics apply: the last ``\\title``/``\\author``/``\\date`` of the project wins. Titles and
dates are filled only as plain best-effort text (Pandoc renders the real title); they are left
untouched in the staged copy. ``\\author{...}`` is parsed here (``\\and`` separates authors, ``\\\\``
separates the lines of one author: name, e-mail address, affiliation lines) and then removed from
the staged copy, because Pandoc would otherwise drop author blocks it does not understand. Any
author content that cannot be converted exactly is an error (never a silent drop).

Keyword environments need no staged edit (Pandoc keeps their text); configured keyword commands
are rewritten into the first configured keyword environment so one Lua filter styles both.

Subclasses (publisher adapters) override :meth:`GenericAdapter.parse_authors` and
:attr:`GenericAdapter.extra_consumed_commands`; everything else is shared.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from latex2word.diagnostics import Severity
from latex2word.latex_scan import read_group, read_optional, scan_environments, skip_ws, split_top_level
from latex2word.metadata.adapters import MetadataExtraction, register_adapter
from latex2word.metadata.model import Affiliation, Author, DocumentMetadata
from latex2word.metadata.tex_text import TexText, tex_to_text
from latex2word.rewrite import Edit
from latex2word.source_graph import Finding, ScannedFile, SourceGraph, SourceLocation

if TYPE_CHECKING:
    from latex2word.layout import FrontmatterConfig, LabelledBlock

_WORD = re.compile(r"[A-Za-z]+")
_ADDRESS = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")

_BASE_CONSUMED = frozenset({"title", "author", "date", "and"})


@dataclass(frozen=True)
class Declaration:
    """One ``\\name[..]{..}`` occurrence; ``content`` is ``None`` when its braces are unbalanced."""

    name: str
    rel: str
    line: int
    start: int  # the backslash
    end: int  # just after the closing brace (or the end of the command word when malformed)
    content: str | None

    @property
    def location(self) -> SourceLocation:
        return SourceLocation(self.rel, self.line)


@dataclass
class ParsedAuthor:
    name: str = ""
    emails: list[str] = field(default_factory=list)
    affiliation_lines: list[str] = field(default_factory=list)

    def add_lines(self, lines: tuple[str, ...]) -> None:
        """Sort display lines into e-mail addresses and affiliation lines."""
        for line in lines:
            if "@" in line:
                self.emails.extend(_ADDRESS.findall(line) or [line])
            else:
                self.affiliation_lines.append(line)

    @classmethod
    def from_lines(cls, lines: tuple[str, ...]) -> ParsedAuthor:
        """First line = name (unless it is an address), then addresses and affiliation lines."""
        author = cls()
        if lines and "@" not in lines[0]:
            author.name, lines = lines[0], lines[1:]
        author.add_lines(lines)
        return author

    @property
    def empty(self) -> bool:
        return not (self.name or self.emails or self.affiliation_lines)


@dataclass
class AuthorParse:
    authors: list[ParsedAuthor] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def find_declarations(rel: str, scanned: ScannedFile, names: frozenset[str] | set[str]) -> list[Declaration]:
    """Occurrences of the named commands that are followed by a braced argument (definitions are not)."""
    masked = scanned.masked
    found: list[Declaration] = []
    i = masked.find("\\")
    while i != -1:
        word = _WORD.match(masked, i + 1)
        if word is None:
            i = masked.find("\\", i + 2)
            continue
        after = word.end()
        if word.group(0) in names:
            declaration = _declaration(rel, scanned, word.group(0), i, after)
            if declaration is not None:
                found.append(declaration)
                after = max(after, declaration.end)
        i = masked.find("\\", after)
    return found


def _declaration(rel: str, scanned: ScannedFile, name: str, start: int, after: int) -> Declaration | None:
    masked = scanned.masked
    pos = skip_ws(masked, after)
    optional = read_optional(masked, pos)
    if optional is not None:
        pos = skip_ws(masked, optional[2])
    if masked[pos : pos + 1] != "{":
        return None
    line = scanned.lines.line(start)
    group = read_group(masked, pos)
    if group is None:
        return Declaration(name, rel, line, start, after, None)
    return Declaration(name, rel, line, start, group[2], masked[group[0] : group[1]])


def split_top_level_command(text: str, name: str) -> list[str]:
    """Split ``text`` on the control word ``\\name`` outside braces."""
    pieces: list[str] = []
    depth, last, i, n = 0, 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            word = _WORD.match(text, i + 1)
            if word is None:
                i += 2
                continue
            if depth == 0 and word.group(0) == name:
                pieces.append(text[last:i])
                last = word.end()
            i = word.end()
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    pieces.append(text[last:])
    return pieces


def plain_text(content: str) -> str | None:
    """One line of exact text, or ``None`` when the fragment is empty or has constructs we cannot convert."""
    converted = tex_to_text(content)
    if converted.problems or not converted.lines:
        return None
    return " ".join(converted.lines)


def assemble(parsed: list[ParsedAuthor]) -> tuple[tuple[Author, ...], tuple[Affiliation, ...], tuple[str, ...]]:
    """Build model authors; identical affiliation line tuples share one ``Affiliation`` (ids ``a1``, ``a2``, ...).

    Returns ``(authors, affiliations, extra_emails)``; addresses beyond an author's first go to the extras.
    """
    ids: dict[tuple[str, ...], str] = {}
    authors: list[Author] = []
    extras: list[str] = []
    for item in parsed:
        lines = tuple(item.affiliation_lines)
        affiliation_ids: tuple[str, ...] = ()
        if lines:
            affiliation_ids = (ids.setdefault(lines, f"a{len(ids) + 1}"),)
        email = item.emails[0] if item.emails else None
        extras.extend(item.emails[1:])
        authors.append(Author(item.name, affiliation_ids, email))
    affiliations = tuple(Affiliation(identifier, lines=lines) for lines, identifier in ids.items())
    return tuple(authors), affiliations, tuple(extras)


class GenericAdapter:
    """``\\author{A \\and B}`` with ``\\\\``-separated name / address / affiliation lines."""

    name = "generic"
    extra_consumed_commands: frozenset[str] = frozenset()

    def parse_authors(self, argument: str) -> AuthorParse:
        result = AuthorParse()
        for fragment in split_top_level_command(argument, "and"):
            converted = tex_to_text(fragment)
            result.problems.extend(converted.problems)
            author = ParsedAuthor.from_lines(converted.lines)
            if not author.empty:
                result.authors.append(author)
        return result

    def extract(self, graph: SourceGraph, frontmatter: FrontmatterConfig) -> MetadataExtraction:
        keywords = frontmatter.keywords
        extraction = MetadataExtraction(
            self.name,
            consumed_commands=_BASE_CONSUMED | self.extra_consumed_commands | frozenset(keywords.commands),
            consumed_environments=frozenset(keywords.environments),
        )
        declarations = _collect(graph, {"title", "author", "date"})
        people = self._authors(declarations, extraction)
        authors, affiliations, extras = people if people is not None else ((), (), ())
        extraction.metadata = DocumentMetadata(
            title=_plain(declarations, "title"),
            date=_plain(declarations, "date"),
            authors=authors,
            affiliations=affiliations,
            keywords=_keywords(graph, keywords, extraction),
            extra_emails=extras,
        )
        return extraction

    def _authors(
        self, declarations: list[Declaration], extraction: MetadataExtraction
    ) -> tuple[tuple[Author, ...], tuple[Affiliation, ...], tuple[str, ...]] | None:
        """Parse the winning ``\\author``; ``None`` (staged source untouched) when it has errors."""
        mine = [d for d in declarations if d.name == "author"]
        if not mine:
            return None
        malformed = [d for d in mine if d.content is None]
        for bad in malformed:
            extraction.findings.append(
                Finding("L2W-E-META-002", Severity.ERROR, "\\author has unbalanced braces; its content cannot be read", bad.location)
            )
        winner = mine[-1]
        parsed = AuthorParse()
        if winner.content is not None:
            parsed = self.parse_authors(winner.content)
            self._report(winner, parsed, extraction)
        if malformed or any(f.severity is Severity.ERROR for f in extraction.findings):
            return None
        extraction.edits.extend(_removal(d) for d in mine)
        result = assemble(parsed.authors)
        extraction.findings.append(
            Finding(
                "L2W-I-META-001", Severity.INFO,
                f"extracted {len(result[0])} author(s) and {len(result[1])} affiliation(s) from \\author", winner.location,
            )
        )
        return result

    @staticmethod
    def _report(winner: Declaration, parsed: AuthorParse, extraction: MetadataExtraction) -> None:
        for problem in dict.fromkeys(parsed.problems):
            extraction.findings.append(
                Finding("L2W-E-META-002", Severity.ERROR, f"\\author content cannot be converted exactly: {problem}", winner.location)
            )
        if not parsed.authors and not parsed.problems and (winner.content or "").strip():
            extraction.findings.append(
                Finding("L2W-E-META-003", Severity.ERROR, "\\author has content but no author could be extracted from it", winner.location)
            )


def _collect(graph: SourceGraph, names: set[str]) -> list[Declaration]:
    found: list[Declaration] = []
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is not None:
            found.extend(find_declarations(rel, scanned, names))
    return found


def _plain(declarations: list[Declaration], name: str) -> str | None:
    """Plain text of the last ``\\name{..}`` (LaTeX: the last one wins), ``None`` when it needs Pandoc."""
    last = next((d for d in reversed(declarations) if d.name == name), None)
    return plain_text(last.content) if last is not None and last.content is not None else None


def _removal(declaration: Declaration) -> Edit:
    return Edit(
        declaration.rel, declaration.start, declaration.end, "", kind="metadata-author",
        reason="the author block is rendered from extracted metadata; Pandoc would drop it silently", subject="author",
    )


def _keyword_texts(content: str) -> list[str] | None:
    """Keywords split on ``,``/``;`` (outside braces) as exact text, or ``None`` when any needs Pandoc (math...)."""
    texts: list[str] = []
    for piece in split_top_level(content, ","):
        for part in split_top_level(piece, ";"):
            converted: TexText = tex_to_text(part)
            if converted.problems:
                return None
            if converted.lines:
                texts.append(" ".join(converted.lines))
    return texts


def _keywords(graph: SourceGraph, keywords: LabelledBlock, extraction: MetadataExtraction) -> tuple[str, ...]:
    """Rewrite keyword commands into the first keyword environment and collect the keyword text."""
    found: list[tuple[int, int, str]] = []  # (file order, offset, raw content)
    if keywords.commands and not keywords.environments:
        extraction.findings.append(
            Finding(
                "L2W-E-META-004", Severity.ERROR,
                "keyword commands are configured but no keyword environment to rewrite them into",
            )
        )
    for order, rel in enumerate(graph.tex_order):
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        if keywords.environments:
            environments, problems = scan_environments(scanned.masked, list(keywords.environments))
            for env in environments:
                found.append((order, env.begin, scanned.masked[env.content_start : env.content_end]))
            for problem in problems:
                extraction.findings.append(
                    Finding("L2W-W-META-002", Severity.WARNING, f"keyword environment: {problem.message}",
                            SourceLocation(rel, scanned.lines.line(problem.offset)))
                )
        if keywords.commands and keywords.environments:
            target = keywords.environments[0]
            for declaration in find_declarations(rel, scanned, set(keywords.commands)):
                if declaration.content is None:
                    continue
                found.append((order, declaration.start, declaration.content))
                extraction.edits.append(
                    Edit(
                        rel, declaration.start, declaration.end,
                        f"\\begin{{{target}}}{declaration.content}\\end{{{target}}}",
                        kind="metadata-keywords", reason=f"\\{declaration.name} rewritten into the {target} environment",
                        subject=declaration.name,
                    )
                )
    texts: list[str] = []
    for _, _, content in sorted(found):
        split = _keyword_texts(content)
        if split is None:
            extraction.findings.append(
                Finding("L2W-I-META-002", Severity.INFO, "keywords contain constructs left to Pandoc; not listed in the metadata")
            )
            continue
        texts.extend(split)
    return tuple(texts)


register_adapter(GenericAdapter())
