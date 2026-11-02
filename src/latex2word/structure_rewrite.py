"""Document-structure markers in the staged copy.

Pandoc's LaTeX reader drops ``\\appendix`` and ``\\printbibliography`` without a
trace, so the semantic filter could not know where the appendices start or
where the bibliography belongs (citeproc then appends it at the very end, after
the appendices). Both commands are replaced in the staged copy by empty marker
environments, which Pandoc keeps as empty ``Div`` blocks:

* ``l2wappendix``: the filter switches to the profile's appendix numbering
  (``numbering.sections.appendix``) and removes the marker;
* ``l2wrefs``: the filter turns it into the ``refs`` division, where citeproc
  places the bibliography, preceded by its heading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from latex2word.diagnostics import Severity
from latex2word.latex_scan import iter_commands, read_group, read_optional, skip_ws, split_top_level
from latex2word.rewrite import Edit
from latex2word.source_graph import EdgeKind, Finding, SourceGraph, SourceLocation

APPENDIX_MARKER = "\\begin{l2wappendix}\\end{l2wappendix}"
REFS_MARKER = "\\begin{l2wrefs}\\end{l2wrefs}"
PAGE_BREAK_MARKER = "\\begin{l2wpagebreak}\\end{l2wpagebreak}"
PAGE_BREAK_COMMANDS = frozenset({"clearpage", "newpage", "cleardoublepage"})
# \pagenumbering{style}: LaTeX page-number style -> Word ST_NumberFormat ("none" for gobble)
PAGE_NUMBER_STYLES = {
    "arabic": "decimal", "roman": "lowerRoman", "Roman": "upperRoman", "alph": "lowerLetter", "Alph": "upperLetter",
    "gobble": "none",
}
# \tableofcontents, \listoffigures, \listoftables -> Word TOC fields written by the filter
CONTENTS_MARKERS = {
    "tableofcontents": ("toc", "\\begin{l2wtoc}\\end{l2wtoc}"),
    "listoffigures": ("lof", "\\begin{l2wlof}\\end{l2wlof}"),
    "listoftables": ("lot", "\\begin{l2wlot}\\end{l2wlot}"),
}
_TITLE_MACROS = {"contentsname": "toc", "listfigurename": "lof", "listtablename": "lot"}
# babel's default names (captions<language>) for the lists latex2word writes
DEFAULT_CONTENTS_TITLES = {
    "english": {"toc": "Contents", "lof": "List of Figures", "lot": "List of Tables"},
    "spanish": {"toc": "Índice general", "lof": "Índice de figuras", "lot": "Índice de cuadros"},
    "french": {"toc": "Table des matières", "lof": "Table des figures", "lot": "Liste des tableaux"},
    "german": {"toc": "Inhaltsverzeichnis", "lof": "Abbildungsverzeichnis", "lot": "Tabellenverzeichnis"},
    "ngerman": {"toc": "Inhaltsverzeichnis", "lof": "Abbildungsverzeichnis", "lot": "Tabellenverzeichnis"},
    "portuguese": {"toc": "Conteúdo", "lof": "Lista de Figuras", "lot": "Lista de Tabelas"},
    "brazilian": {"toc": "Sumário", "lof": "Lista de Figuras", "lot": "Lista de Tabelas"},
    "italian": {"toc": "Indice", "lof": "Elenco delle figure", "lot": "Elenco delle tabelle"},
    "catalan": {"toc": "Índex", "lof": "Índex de figures", "lot": "Índex de taules"},
}
_BEGIN_DOCUMENT = re.compile(r"\\begin\s*\{document\}")


@dataclass
class StructurePlan:
    edits: list[Edit] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    bibliography_title: str | None = None  # title= of \printbibliography, when given
    refs_marker: bool = False
    contents: list[str] = field(default_factory=list)  # "toc" | "lof" | "lot", in document order
    page_numbering: int = 0  # \pagenumbering / \setcounter{page} markers written
    toc_levels: dict = field(default_factory=dict)  # sectioning unit -> TOC level (chapter 1 when chapters exist)
    bibliography_in_toc: bool = True  # \printbibliography[heading=bibintoc|subbibintoc]; other headings: not in the TOC
    custom_lists: list = field(default_factory=list)  # CustomList entries (tocloft newlistof)


def _option_value(options: str, key: str) -> str | None:
    for item in split_top_level(options):
        name, equals, value = item.partition("=")
        if name.strip() == key and equals:
            value = value.strip()
            if value.startswith("{") and value.endswith("}"):
                value = value[1:-1]
            return " ".join(value.split())
    return None


def _page_number_marker(name: str, masked: str, text: str, command, where: SourceLocation, plan: StructurePlan) -> Edit | None:
    """``\\pagenumbering{style}`` -> ``l2wpn-<WordFormat>``; ``\\setcounter{page}{n}`` -> ``l2wpage-<n>``."""
    first = read_group(masked, skip_ws(masked, command.end))
    if first is None:
        return None
    value = text[first[0] : first[1]].strip()
    if name == "pagenumbering":
        if value not in PAGE_NUMBER_STYLES:
            plan.findings.append(Finding("L2W-W-PAGE-001", Severity.WARNING,
                                         f"\\pagenumbering{{{value}}} is not supported; the numbering style is unchanged", where))
            return None
        marker = f"l2wpn-{PAGE_NUMBER_STYLES[value]}"
        end = first[2]
    else:
        if value != "page":
            return None
        second = read_group(masked, skip_ws(masked, first[2]))
        if second is None:
            return None
        number = text[second[0] : second[1]].strip()
        if not number.isdigit():
            plan.findings.append(Finding("L2W-W-PAGE-002", Severity.WARNING,
                                         f"\\setcounter{{page}}{{{number}}}: only a literal number is supported", where))
            return None
        marker = f"l2wpage-{int(number)}"
        end = second[2]
    plan.page_numbering += 1
    return Edit(where.file, command.start, end, f"\\begin{{{marker}}}\\end{{{marker}}}", "structure-pagination",
                f"\\{name} starts a Word section with its own page numbering (Pandoc drops the command)", marker)


# tocloft custom lists (\newlistof{name}{ext}{title}): entries reach Word as TC fields with a list
# identifier, \listof<name> as a TOC \f field. These staged-preamble definitions turn every
# \addcontentsline (also inside user macros, after Pandoc's macro expansion) into a marker block.
CONTENTS_LINE_PREAMBLE = (
    "\n% latex2word: contents-list entries as marker blocks (custom lists become Word TC fields)\n"
    "\\renewcommand{\\addcontentsline}[3]{\\begin{l2wcontentsline}\\begin{l2wcontentsext}#1\\end{l2wcontentsext}"
    "\\begin{l2wcontentsunit}#2\\end{l2wcontentsunit}#3\\end{l2wcontentsline}}\n"
    "\\renewcommand{\\numberline}[1]{\\begin{l2wnumberline}\\end{l2wnumberline}}\n"
)
_LIST_LETTERS = "LMNOPQRSUVWXYZ"


@dataclass(frozen=True)
class CustomList:
    name: str
    ext: str
    title: str
    letter: str


def custom_lists(graph: SourceGraph) -> list[CustomList]:
    """``\\newlistof{name}{ext}{title}`` of the main preamble; a title given as a macro is resolved one level."""
    main = graph.files.get(graph.main)
    if main is None:
        return []
    masked, text = main.masked, main.text
    begin = _BEGIN_DOCUMENT.search(masked)
    limit = begin.start() if begin else len(masked)
    macros: dict[str, str] = {}
    found: list[tuple[str, str, str]] = []
    for command in iter_commands(masked[:limit]):
        if command.name in ("newcommand", "renewcommand", "providecommand"):
            pos = skip_ws(masked, command.end)
            group = read_group(masked, pos)
            if group is None:
                continue
            name = masked[group[0] : group[1]].strip().lstrip("\\")
            body = read_group(masked, skip_ws(masked, group[2]))
            if body is not None and name.isalpha():
                macros[name] = " ".join(text[body[0] : body[1]].split())
        elif command.name == "newlistof":
            args, pos = [], command.end
            if read_optional(masked, skip_ws(masked, pos)) is not None:
                pos = read_optional(masked, skip_ws(masked, pos))[2]
            for _ in range(3):
                group = read_group(masked, skip_ws(masked, pos))
                if group is None:
                    break
                args.append(" ".join(text[group[0] : group[1]].split()))
                pos = group[2]
            if len(args) == 3:
                found.append((args[0], args[1], args[2]))
    lists = []
    for index, (name, ext, title) in enumerate(found[: len(_LIST_LETTERS)]):
        macro = re.fullmatch(r"\\([A-Za-z]+)\s*", title)
        if macro and macro.group(1) in macros:
            title = macros[macro.group(1)]
        lists.append(CustomList(name, ext, title, _LIST_LETTERS[index]))
    return lists


def plan_structure_rewrites(
    graph: SourceGraph, *, place_bibliography: bool = False, follow_pagination: bool = False
) -> StructurePlan:
    """Markers for ``\\appendix``, page breaks, contents lists, ``\\pagenumbering`` / ``\\setcounter{page}``
    (with ``follow_pagination``) and, with ``place_bibliography``, the first ``\\printbibliography``."""
    plan = StructurePlan()
    plan.custom_lists = custom_lists(graph)
    list_commands = {f"listof{c.name}": c for c in plan.custom_lists}
    main = graph.files.get(graph.main)
    commands = {c.name for f in graph.files.values() for c in iter_commands(f.masked)}
    # \addcontentsline{toc}{<unit>}{..}: TOC level of each sectioning unit, as the class numbers them
    units = ("chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph")
    if "chapter" not in commands:
        units = units[1:]
    plan.toc_levels = {unit: level for level, unit in enumerate(units, start=1)}
    plan.toc_levels["part"] = 1
    if (plan.custom_lists or "addcontentsline" in commands) and main is not None:
        begin = _BEGIN_DOCUMENT.search(main.masked)
        if begin is not None:
            plan.edits.append(Edit(graph.main, begin.start(), begin.start(), CONTENTS_LINE_PREAMBLE, "structure-contents",
                                   "contents-list entries become marker blocks (manual TOC entries, tocloft lists)",
                                   "addcontentsline"))
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked = scanned.masked
        body_start = 0
        if rel == graph.main:
            begin = _BEGIN_DOCUMENT.search(masked)
            if begin is None:
                continue
            body_start = begin.end()
        for command in iter_commands(masked):
            if command.start < body_start:
                continue
            if command.name in list_commands:
                custom = list_commands[command.name]
                plan.contents.append(f"list-{custom.letter}")
                marker = f"l2wlistof-{custom.letter}"
                plan.edits.append(
                    Edit(rel, command.start, command.end, f"\\begin{{{marker}}}\\end{{{marker}}}", "structure-contents",
                         f"\\{command.name} (tocloft list) becomes a Word TOC field over its TC entries", marker)
                )
            elif command.name in ("pagenumbering", "setcounter") and follow_pagination:
                where = SourceLocation(rel, scanned.lines.line(command.start))
                edit = _page_number_marker(command.name, masked, scanned.text, command, where, plan)
                if edit is not None:
                    plan.edits.append(edit)
            elif command.name in PAGE_BREAK_COMMANDS:
                plan.edits.append(
                    Edit(rel, command.start, command.end, PAGE_BREAK_MARKER, "structure-pagebreak",
                         f"\\{command.name} becomes a Word page break (Pandoc drops the command)", command.name)
                )
            elif command.name in CONTENTS_MARKERS:
                kind, marker = CONTENTS_MARKERS[command.name]
                plan.contents.append(kind)
                plan.edits.append(
                    Edit(rel, command.start, command.end, marker, "structure-contents",
                         f"\\{command.name} becomes a Word TOC field (Pandoc drops the command)", kind)
                )
            elif command.name == "appendix":
                plan.edits.append(
                    Edit(rel, command.start, command.end, APPENDIX_MARKER, "structure-appendix",
                         "\\appendix marked for the filter (Pandoc drops the command)", "appendix")
                )
            elif command.name == "printbibliography" and place_bibliography:
                end = command.end
                optional = read_optional(masked, skip_ws(masked, end))
                options = ""
                if optional is not None:
                    options = scanned.text[optional[0] : optional[1]]
                    end = optional[2]
                where = SourceLocation(rel, scanned.lines.line(command.start))
                if plan.refs_marker:
                    plan.findings.append(Finding(
                        "L2W-W-BIB-020", Severity.WARNING,
                        "a further \\printbibliography is ignored: the bibliography is placed once, at the first one", where))
                    continue
                plan.refs_marker = True
                plan.bibliography_title = _option_value(options, "title")
                heading = _option_value(options, "heading")
                plan.bibliography_in_toc = heading in ("bibintoc", "subbibintoc", "bibnumbered", "subbibnumbered")
                plan.edits.append(
                    Edit(rel, command.start, end, REFS_MARKER, "structure-bibliography",
                         "\\printbibliography marks where citeproc places the bibliography", "bibliography")
                )
    return plan


def contents_titles(graph: SourceGraph, language: str | None) -> dict[str, str]:
    """Titles of the contents lists: the document's renewed names, else babel's defaults for ``language``."""
    titles = dict(DEFAULT_CONTENTS_TITLES.get(language or "english", DEFAULT_CONTENTS_TITLES["english"]))
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked, text = scanned.masked, scanned.text
        for command in iter_commands(masked):
            if command.name not in ("renewcommand", "def"):
                continue
            pos = skip_ws(masked, command.end)
            group = read_group(masked, pos)
            if group is not None:
                name = masked[group[0] : group[1]].strip().lstrip("\\")
                pos = group[2]
            else:  # \renewcommand\listfigurename{...}
                match = re.match(r"\\([A-Za-z]+)", masked[pos:])
                if match is None:
                    continue
                name = match.group(1)
                pos += match.end()
            if name not in _TITLE_MACROS:
                continue
            body = read_group(masked, skip_ws(masked, pos))
            if body is not None:
                titles[_TITLE_MACROS[name]] = " ".join(text[body[0] : body[1]].split())
    return titles


# ------------------------------------------------------------------- front-matter typesetting
# Pandoc drops relative size commands, \vspace, \hrulefill and \null. On their own lines in running text
# (title pages, signature pages, front matter) they become markers the filter turns into a character
# style, a fixed-height spacer paragraph, a bottom-bordered rule paragraph and a blank first page.
SIZE_COMMANDS = ("tiny", "scriptsize", "footnotesize", "small", "normalsize", "large", "Large", "LARGE", "huge", "Huge")
_NON_TEXT_ENVIRONMENTS = (
    "tabular", "tabular*", "tabularx", "tabulary", "longtable", "array", "tikzpicture", "circuitikz", "figure",
    "figure*", "table", "table*", "minipage", "equation", "equation*", "align", "align*", "gather", "gather*",
    "multline", "multline*", "verbatim", "lstlisting",
)


def _line_bounds(masked: str, start: int, end: int) -> bool:
    """Only whitespace between the line start and ``start`` and between ``end`` and the line end."""
    line_start = masked.rfind("\n", 0, start) + 1
    line_end = masked.find("\n", end)
    line_end = len(masked) if line_end < 0 else line_end
    return not masked[line_start:start].strip() and not masked[end:line_end].strip()


def tex_length_twips(text: str, base_pt: float) -> int | None:
    """A literal TeX length in twips (in, cm, mm, pt, bp; em = the base font size); None when not literal."""
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?|-?\.\d+)\s*(in|cm|mm|pt|bp|em)\s*", text)
    if match is None:
        return None
    value, unit = float(match.group(1)), match.group(2)
    points = {"in": 72.0, "cm": 72 / 2.54, "mm": 72 / 25.4, "pt": 72 / 72.27, "bp": 1.0, "em": base_pt}[unit] * value
    return round(points * 20)


# where the main matter starts: \mainmatter, \pagenumbering{arabic}, or the first numbered \part/\chapter/\section
_MAIN_MATTER = re.compile(
    r"\\(?:mainmatter\b|pagenumbering\s*\{\s*arabic\s*\}|(?:part|chapter|section)\s*[\[{])"
)


def front_matter_scope(graph: SourceGraph) -> dict[str, int]:
    """For each file of the front matter, the offset where the main matter starts in it (or its length).

    A file belongs to the front matter when it is the main file or is input from a parent's front-matter
    region; files input after the main-matter start are absent from the result.
    """
    scope: dict[str, int] = {}
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        if rel != graph.main:
            sites = [
                e for e in graph.edges_of(EdgeKind.INPUT, EdgeKind.INCLUDE)
                if e.target == rel and e.source in scope and e.offset < scope[e.source]
            ]
            if not sites:
                continue
        start = 0
        if rel == graph.main:
            begin = _BEGIN_DOCUMENT.search(scanned.masked)
            start = begin.end() if begin is not None else len(scanned.masked)
        boundary = _MAIN_MATTER.search(scanned.masked, start)
        scope[rel] = boundary.start() if boundary is not None else len(scanned.masked)
    return scope


def plan_typesetting_rewrites(graph: SourceGraph, base_pt: float) -> tuple[list[Edit], list[Finding]]:
    """Markers for line-standalone size groups, \\vspace, \\hrulefill and \\null of the front matter.

    Only the front matter (title page, signature page, preliminary chapters) is rewritten; the main
    matter keeps its established conversion.
    """
    from latex2word.latex_scan import scan_environments

    edits: list[Edit] = []
    findings: list[Finding] = []
    scope = front_matter_scope(graph)
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None or rel not in scope:
            continue
        masked, text = scanned.masked, scanned.text
        limit = scope[rel]
        body_start = 0
        if rel == graph.main:
            begin = _BEGIN_DOCUMENT.search(masked)
            if begin is None:
                continue
            body_start = begin.end()
        environments, _ = scan_environments(masked, _NON_TEXT_ENVIRONMENTS)
        excluded = [(env.begin, env.end) for env in environments]
        math = [(m.start(), m.end()) for m in re.finditer(r"\$\$.*?\$\$|\$[^$]*\$|\\\[.*?\\\]|\\\(.*?\\\)", masked, re.S)]

        def free(position: int) -> bool:
            return body_start <= position < limit and not any(a <= position < b for a, b in excluded + math)

        for command in iter_commands(masked):
            if not free(command.start):
                continue
            where = SourceLocation(rel, scanned.lines.line(command.start))
            name = command.name
            if name in SIZE_COMMANDS:
                # {\Large ...} as a paragraph of its own: the whole group takes the size
                brace = command.start - 1
                while brace >= 0 and masked[brace] in " \t":
                    brace -= 1
                if brace < 0 or masked[brace] != "{":
                    continue
                group = read_group(masked, brace)
                if group is None or not _line_bounds(masked, brace, group[2]):
                    continue
                marker = f"l2wsize-{name}"
                # two small edits (opening and closing brace) so rewrites inside the group are kept
                edits.append(Edit(rel, brace, command.end, f"\\begin{{{marker}}}{{", "frontmatter-size",
                                  f"{{\\{name} ...}} group: character size of the profile", name))
                edits.append(Edit(rel, group[1], group[2], f"}}\\end{{{marker}}}", "frontmatter-size",
                                  f"end of the {{\\{name} ...}} group", name))
            elif name == "vspace":
                pos = command.end
                if masked[pos : pos + 1] == "*":
                    pos += 1
                group = read_group(masked, skip_ws(masked, pos))
                if group is None or not _line_bounds(masked, command.start, group[2]):
                    continue
                twips = tex_length_twips(text[group[0] : group[1]], base_pt)
                if twips is None or twips <= 0:
                    findings.append(Finding("L2W-W-SPACE-001", Severity.WARNING,
                                            f"\\vspace{{{text[group[0]:group[1]]}}}: not a literal positive length; ignored",
                                            where))
                    continue
                marker = f"l2wvspace-{twips}"
                edits.append(Edit(rel, command.start, group[2], f"\\begin{{{marker}}}\\end{{{marker}}}",
                                  "frontmatter-vspace", "\\vspace becomes a spacer paragraph of the same height", "vspace"))
            elif name == "hrulefill":
                end = command.end
                after = skip_ws(masked, end)
                if masked.startswith("\\\\", after):
                    end = after + 2
                    optional = read_optional(masked, skip_ws(masked, end))
                    if optional is not None:
                        end = optional[2]
                if not _line_bounds(masked, command.start, end):
                    continue
                edits.append(Edit(rel, command.start, end, "\\begin{l2whrule}\\end{l2whrule}", "frontmatter-rule",
                                  "\\hrulefill on its own line becomes a full-width rule paragraph", "hrulefill"))
            elif name == "null":
                if _line_bounds(masked, command.start, command.end):
                    edits.append(Edit(rel, command.start, command.end, "\\begin{l2wnull}\\end{l2wnull}",
                                      "frontmatter-null", "\\null (an empty box: makes a following page break real)",
                                      "null"))
    return edits, findings


# --------------------------------------------------------------------------- figure/table notes
#
# A note is a paragraph-level formatted block attached to a figure or table: a brace group that starts with
# declarations (at least one size command, optionally alignment) and ends its paragraph, e.g.
#     \newcommand{\note}[1]{\par\medskip{\raggedright\footnotesize\textit{Note.} #1\par}}
# It is recognised only where LaTeX typesets such a block as part of a float: inside a figure/table environment
# (not inside a table cell) or right after a table environment. Ordinary prose, captions, cells and size groups
# elsewhere are never touched. The block is wrapped in a marker environment carrying the source semantics; the
# semantic filter gives it the paragraph style "Caption Note" and the OOXML pass sets that style from them.

_FLOAT_ENVIRONMENTS = (
    "figure", "figure*", "table", "table*", "wrapfigure", "wraptable", "sidewaysfigure", "sidewaystable",
    "SCfigure", "SCtable",
)
_TABLE_ENVIRONMENTS = (
    "tabular", "tabular*", "tabularx", "tabulary", "longtable", "xltabular", "supertabular", "longtabu", "tabu",
)
_ALIGN_DECLARATIONS = {"raggedright": "l", "RaggedRight": "l", "centering": "c", "raggedleft": "r", "RaggedLeft": "r"}
_SKIPS_PT = {"smallskip": 3.0, "medskip": 6.0, "bigskip": 12.0}
_DEFINE_COMMANDS = ("newcommand", "renewcommand", "providecommand")


@dataclass(frozen=True)
class NoteShape:
    """Source semantics of a note block."""

    size: str  # LaTeX size command
    align: str  # l / c / r, or j: the paragraph alignment is inherited
    no_indent: bool  # the declarations set \parindent to 0 (\raggedright, \centering, \raggedleft, \noindent)
    skip_twips: int  # vertical skip before the block (\medskip, \vspace{..})

    def marker(self, context: str) -> str:
        """Marker environment name; ``context`` is ``f`` (inside a float: single-spaced) or ``o`` (outside)."""
        return f"l2wnote-{self.size}-{self.align}-{'n' if self.no_indent else 'i'}-{self.skip_twips}-{context}"


def _skip_before(masked: str, pos: int, end: int, base_pt: float) -> tuple[int, int] | None:
    """Vertical material at ``[pos, end)``: only ``\\par``, ``\\smallskip``/``\\medskip``/``\\bigskip`` and literal
    ``\\vspace``; returns ``(twips, position after it)``, or ``None`` when anything else is there."""
    twips = 0
    pos = skip_ws(masked, pos)
    while pos < end and masked.startswith("\\", pos):
        command = next(iter_commands(masked, pos), None)
        if command is None or command.start != pos:
            return None
        if command.name == "par":
            pos = command.end
        elif command.name in _SKIPS_PT:
            twips += round(_SKIPS_PT[command.name] * 20)
            pos = command.end
        elif command.name in ("vspace", "addvspace"):
            after = command.end + (1 if masked[command.end : command.end + 1] == "*" else 0)
            group = read_group(masked, skip_ws(masked, after))
            length = tex_length_twips(masked[group[0] : group[1]], base_pt) if group is not None else None
            if length is None:
                return None
            twips += length
            pos = group[2]
        else:
            break
        pos = skip_ws(masked, pos)
    return twips, pos


def _group_shape(masked: str, brace: int) -> tuple[str, str, bool, int] | None:
    """``(size, align, no_indent, group end)`` when the ``{..}`` at ``brace`` is a note-shaped group: it starts with
    declarations including a size command and its content ends with ``\\par``."""
    group = read_group(masked, brace)
    if group is None:
        return None
    content_start, content_end, after = group
    content = masked[content_start:content_end]
    if not re.search(r"\\par\s*$", content):
        return None
    if re.search(r"\\(?:caption|includegraphics)\b|\\begin\s*\{(?:" + "|".join(
        re.escape(name) for name in (*_FLOAT_ENVIRONMENTS, *_TABLE_ENVIRONMENTS, "minipage")
    ) + r")\}", content):
        return None  # a size group around a table, a figure or a caption is not a note
    size, align, no_indent = None, "j", False
    pos = skip_ws(masked, content_start)
    for command in iter_commands(masked, pos):
        if command.start != pos:
            break
        if command.name in SIZE_COMMANDS:
            size = command.name
        elif command.name in _ALIGN_DECLARATIONS:
            align, no_indent = _ALIGN_DECLARATIONS[command.name], True
        elif command.name == "noindent":
            no_indent = True
        else:
            break
        pos = skip_ws(masked, command.end)
    if size is None:
        return None
    return size, align, no_indent, after


def note_macros(graph: SourceGraph, base_pt: float = 10) -> dict[str, tuple[int, bool, NoteShape]]:
    """User macros whose whole body is a note block: name -> (arguments, has optional argument, shape)."""
    macros: dict[str, tuple[int, bool, NoteShape]] = {}
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked = scanned.masked
        for command in iter_commands(masked):
            if command.name not in _DEFINE_COMMANDS:
                continue
            pos = skip_ws(masked, command.end)
            name_group = read_group(masked, pos)
            if name_group is not None:
                name = masked[name_group[0] : name_group[1]].strip()
                pos = name_group[2]
            else:
                defined = next(iter_commands(masked, pos), None)
                if defined is None or defined.start != pos:
                    continue
                name, pos = masked[defined.start : defined.end], defined.end
            if not re.fullmatch(r"\\[A-Za-z]+", name):
                continue
            count, has_optional = 0, False
            optional = read_optional(masked, skip_ws(masked, pos))
            if optional is not None:
                digits = masked[optional[0] : optional[1]].strip()
                count = int(digits) if digits.isdigit() else -1
                pos = optional[2]
                default = read_optional(masked, skip_ws(masked, pos))
                if default is not None:
                    has_optional, pos = True, default[2]
            body = read_group(masked, skip_ws(masked, pos))
            if count < 0 or body is None:
                continue
            skip = _skip_before(masked, body[0], body[1], base_pt)
            if skip is None or skip[1] >= body[1] or masked[skip[1]] != "{":
                continue
            shape = _group_shape(masked, skip[1])
            if shape is None or masked[shape[3] : body[1]].strip():
                continue  # anything after the group: not a pure note block
            macros[name[1:]] = (count, has_optional, NoteShape(shape[0], shape[1], shape[2], skip[0]))
    return macros


def _note_context(
    position: int, floats: list[tuple[int, int]], tables: list[tuple[int, int]], masked: str
) -> str | None:
    """``f`` inside a float (not in a table cell), ``o`` right after the end of a table environment, else ``None``."""
    if any(a <= position < b for a, b in tables):
        return None
    if any(a <= position < b for a, b in floats):
        return "f"
    before = masked[:position].rstrip()
    before = re.sub(r"(\\(?:par|smallskip|medskip|bigskip)\s*)+$", "", before).rstrip()
    return "o" if any(before.endswith(f"\\end{{{name}}}") for name in _TABLE_ENVIRONMENTS) else None


def plan_note_rewrites(graph: SourceGraph, base_pt: float) -> tuple[list[Edit], list[tuple[str, int, int]]]:
    """Marker environments around the note blocks of figures and tables (see the section comment).

    Returns the edits and the wrapped spans ``(file, start, end)`` (other typesetting rewrites must stay out).
    Each block gets two small edits (opening and closing) so rewrites inside the note text are kept.
    """
    from latex2word.latex_scan import scan_environments

    macros = note_macros(graph, base_pt)
    edits: list[Edit] = []
    spans: list[tuple[str, int, int]] = []
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked = scanned.masked
        body_start = 0
        if rel == graph.main:
            begin = _BEGIN_DOCUMENT.search(masked)
            if begin is None:
                continue
            body_start = begin.end()
        floats = [(e.begin, e.end) for e in scan_environments(masked, _FLOAT_ENVIRONMENTS)[0]]
        tables = [(e.begin, e.end) for e in scan_environments(masked, _TABLE_ENVIRONMENTS)[0]]
        consumed = 0
        for command in iter_commands(masked, body_start):
            if command.start < consumed:
                continue
            if command.name in macros:
                count, has_optional, shape = macros[command.name]
                pos = command.end
                if has_optional:
                    optional = read_optional(masked, skip_ws(masked, pos))
                    if optional is not None:
                        pos = optional[2]
                last = None
                for _ in range(count):
                    last = read_group(masked, skip_ws(masked, pos))
                    if last is None:
                        break
                    pos = last[2]
                context = _note_context(command.start, floats, tables, masked)
                if (count and last is None) or context is None:
                    continue
                marker = shape.marker(context)
                name_text = masked[command.start : command.end]
                closing = f"\\end{{{marker}}}" if not count else ""
                edits.append(Edit(rel, command.start, command.end, f"\\begin{{{marker}}}{name_text}{closing}",
                                  "note-block", f"\\{command.name}: note block of a figure/table", command.name))
                if count:  # the closing marker replaces the last argument's closing brace
                    edits.append(Edit(rel, pos - 1, pos, f"}}\\end{{{marker}}}", "note-block",
                                      f"end of the \\{command.name} note block", command.name))
                spans.append((rel, command.start, pos))
                consumed = pos
            elif masked[command.start - 1 : command.start] == "{" or command.name in _SKIPS_PT or command.name in ("vspace", "par"):
                # a literal note group: optional skips, then {<declarations> ... \par}
                start = command.start - 1 if masked[command.start - 1 : command.start] == "{" else command.start
                skip = (0, start) if masked[start] == "{" else _skip_before(masked, start, len(masked), base_pt)
                if skip is None or skip[1] >= len(masked) or masked[skip[1]] != "{":
                    continue
                shape = _group_shape(masked, skip[1])
                context = _note_context(skip[1], floats, tables, masked) if shape is not None else None
                if shape is None or context is None:
                    continue
                marker = NoteShape(shape[0], shape[1], shape[2], skip[0]).marker(context)
                brace, end = skip[1], shape[3]
                edits.append(Edit(rel, brace, brace + 1, f"\\begin{{{marker}}}{{", "note-block",
                                  "note group of a figure/table", shape[0]))
                edits.append(Edit(rel, end - 1, end, f"}}\\end{{{marker}}}", "note-block",
                                  "end of the note group", shape[0]))
                spans.append((rel, brace, end))
                consumed = end
    return edits, spans
