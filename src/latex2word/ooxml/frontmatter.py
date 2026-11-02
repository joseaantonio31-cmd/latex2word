"""Front-matter rendering on the Pandoc ``w:document`` tree: author block, abstract and keyword labels.

The metadata model is the source of truth for authors: Pandoc's own ``Author`` paragraphs are removed and the
block is rendered from :class:`DocumentMetadata`, so every publisher variation (stacked list, borderless grid)
is a matter of :class:`FrontmatterConfig` data. Labels and separators come from the config; nothing here knows
a publisher or a literal such as "Abstract". Math and every foreign run inside the abstract stay untouched
(the label is only *inserted*), so OMML and bookmark anchors survive.

Diagnostics use ``L2W-{W,I,E}-LAYOUT-040..059``:

* ``L2W-W-LAYOUT-040`` the document has no ``Title`` paragraph (none is invented);
* ``L2W-W-LAYOUT-041`` a Pandoc ``Author`` paragraph names someone who is not in the metadata.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from latex2word.diagnostics import Severity
from latex2word.layout import FrontmatterConfig, LabelledBlock, ResolvedLayout
from latex2word.metadata.model import Author, DocumentMetadata
from latex2word.ooxml import style_ids as styles
from latex2word.ooxml.namespaces import qn, text_of, w
from latex2word.ooxml.package import OpcPackage
from latex2word.ooxml.xmlutil import (
    RPR_ORDER,
    TBLPR_ORDER,
    TCPR_ORDER,
    TRPR_ORDER,
    paragraph_style,
    set_child,
    set_paragraph_style,
)
from latex2word.source_graph import Finding

_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_CORE_PART = "docProps/core.xml"
_NAME_SPLIT = re.compile(r"[,;\n\t]|\band\b|&")


@dataclass
class FrontmatterOutcome:
    """What the pass did, for the build report and the validators."""

    title_present: bool = False
    title_text: str = ""
    author_count_requested: int = 0
    author_count_rendered: int = 0
    author_layout: str = "none"  # stacked | columns | none
    author_rows: int = 0
    author_columns: int = 0
    author_names: tuple[str, ...] = ()
    abstract_present: bool = False
    abstract_layout: str = ""
    keywords_present: bool = False
    keywords_layout: str = ""
    date_hidden: bool = False
    removed_author_paragraphs: int = 0
    last_frontmatter_element_hint: None = field(default=None)  # the orchestrator derives the section break

    def to_dict(self) -> dict[str, Any]:
        return {
            "title_present": self.title_present,
            "title_text": self.title_text,
            "author_count_requested": self.author_count_requested,
            "author_count_rendered": self.author_count_rendered,
            "author_layout": self.author_layout,
            "author_rows": self.author_rows,
            "author_columns": self.author_columns,
            "author_names": list(self.author_names),
            "abstract_present": self.abstract_present,
            "abstract_layout": self.abstract_layout,
            "keywords_present": self.keywords_present,
            "keywords_layout": self.keywords_layout,
            "date_hidden": self.date_hidden,
            "removed_author_paragraphs": self.removed_author_paragraphs,
            "last_frontmatter_element_hint": self.last_frontmatter_element_hint,
        }


# -- element builders ----------------------------------------------------------------------------------


def _text_run(text: str, run_style: str | None = None) -> etree._Element:
    run = etree.Element(w("r"))
    if run_style:
        set_child(etree.SubElement(run, w("rPr")), "rStyle", RPR_ORDER, val=run_style)
    node = etree.SubElement(run, w("t"))
    node.text = text
    node.set(_XML_SPACE, "preserve")
    return run


def _paragraph(style: str | None = None) -> etree._Element:
    paragraph = etree.Element(w("p"))
    if style:
        set_paragraph_style(paragraph, style)
    return paragraph


def _lines_paragraph(style: str, lines: tuple[str, ...]) -> etree._Element:
    """One paragraph whose ``lines`` are separated by soft line breaks (``w:br``)."""
    paragraph = _paragraph(style)
    for index, line in enumerate(lines):
        if index:
            etree.SubElement(etree.SubElement(paragraph, w("r")), w("br"))
        paragraph.append(_text_run(line))
    return paragraph


def _author_paragraphs(author: Author, metadata: DocumentMetadata, config: FrontmatterConfig) -> list[etree._Element]:
    paragraphs = [_lines_paragraph(styles.AUTHOR, (author.name,))]
    if config.author_block.show_affiliation:
        lines = tuple(
            line for affiliation in metadata.author_affiliations(author) for line in affiliation.display_lines()
        )
        if lines:
            paragraphs.append(_lines_paragraph(styles.AUTHOR_AFFILIATION, lines))
    if config.author_block.show_email and author.email:
        paragraphs.append(_lines_paragraph(styles.AUTHOR_EMAIL, (author.email,)))
    return paragraphs


def _split_evenly(total: int, parts: int) -> list[int]:
    """``total`` split into ``parts`` integers summing exactly to ``total`` (the remainder goes to the first ones)."""
    base, extra = divmod(total, parts)
    return [base + (1 if index < extra else 0) for index in range(parts)]


def _author_table(cells: list[list[etree._Element]], rows: int, columns: int, width_twips: int) -> etree._Element:
    """A borderless fixed-layout grid of ``rows`` x ``columns`` cells filled row-major; missing cells are padding."""
    table = etree.Element(w("tbl"))
    properties = etree.SubElement(table, w("tblPr"))
    set_child(properties, "tblStyle", TBLPR_ORDER, val=styles.TABLE_AUTHOR_BLOCK)
    set_child(properties, "tblW", TBLPR_ORDER, w=str(width_twips), type="dxa")
    set_child(properties, "jc", TBLPR_ORDER, val="center")
    set_child(properties, "tblLayout", TBLPR_ORDER, type="fixed")
    widths = _split_evenly(width_twips, columns)
    grid = etree.SubElement(table, w("tblGrid"))
    for column_width in widths:
        etree.SubElement(grid, w("gridCol")).set(w("w"), str(column_width))
    for row_index in range(rows):
        row = etree.SubElement(table, w("tr"))
        set_child(etree.SubElement(row, w("trPr")), "cantSplit", TRPR_ORDER)
        for column_index in range(columns):
            index = row_index * columns + column_index
            cell = etree.SubElement(row, w("tc"))
            cell_properties = etree.SubElement(cell, w("tcPr"))
            set_child(cell_properties, "tcW", TCPR_ORDER, w=str(widths[column_index]), type="dxa")
            set_child(cell_properties, "vAlign", TCPR_ORDER, val="top")
            content = cells[index] if index < len(cells) else [_paragraph(styles.AUTHOR)]
            for paragraph in content:
                cell.append(paragraph)
    return table


# -- body navigation -----------------------------------------------------------------------------------


def _is_paragraph(element: etree._Element) -> bool:
    return element.tag == w("p")


def _styled(body: etree._Element, style: str) -> list[etree._Element]:
    return [child for child in body if _is_paragraph(child) and paragraph_style(child) == style]


def _front_boundary(body: etree._Element) -> int:
    """Index of the first abstract/heading paragraph: Pandoc metadata paragraphs all sit before it."""
    stops = {styles.ABSTRACT_TITLE, styles.ABSTRACT, *styles.HEADING}
    for index, child in enumerate(body):
        if _is_paragraph(child) and paragraph_style(child) in stops:
            return index
    return len(body)


def _unknown_names(text: str, metadata: DocumentMetadata) -> list[str]:
    known = [author.name.casefold() for author in metadata.authors]
    unknown: list[str] = []
    for candidate in _NAME_SPLIT.split(text):
        fragment = candidate.strip()
        folded = fragment.casefold()
        if fragment and not any(name in folded or folded in name for name in known):
            unknown.append(fragment)
    return unknown


# -- steps ---------------------------------------------------------------------------------------------


def _remove_metadata_paragraphs(
    body: etree._Element, metadata: DocumentMetadata, config: FrontmatterConfig, outcome: FrontmatterOutcome,
    findings: list[Finding],
) -> None:
    """Drop Pandoc's ``Author`` paragraphs (the metadata renders them) and ``Date`` paragraphs when hidden."""
    boundary = _front_boundary(body)
    outcome.date_hidden = not config.show_date
    for child in list(body)[:boundary]:
        if not _is_paragraph(child):
            continue
        style = paragraph_style(child)
        if style == styles.DATE and not config.show_date:
            body.remove(child)
        elif style == styles.AUTHOR:
            text = text_of(child).strip()
            if text:
                outcome.removed_author_paragraphs += 1
                unknown = _unknown_names(text, metadata)
                if unknown:
                    findings.append(Finding(
                        "L2W-W-LAYOUT-041", Severity.WARNING,
                        f"A Pandoc author paragraph names {', '.join(repr(u) for u in unknown)}, "
                        "which is not in the author metadata; it was replaced by the rendered author block.",
                    ))
            body.remove(child)


def _ensure_paragraph_follows(table: etree._Element) -> None:
    """Word requires a paragraph after a table; a minimal empty one also separates adjacent tables."""
    following = table.getnext()
    if following is None or not _is_paragraph(following):
        table.addnext(_paragraph())


def _insert_author_block(
    body: etree._Element, anchor: int, metadata: DocumentMetadata, layout: ResolvedLayout,
    config: FrontmatterConfig, outcome: FrontmatterOutcome,
) -> None:
    authors = metadata.authors
    outcome.author_count_requested = len(authors)
    if not authors:
        return
    blocks = [_author_paragraphs(author, metadata, config) for author in authors]
    outcome.author_count_rendered = len(authors)
    outcome.author_names = tuple(author.name for author in authors)
    if config.author_block.layout == "columns":
        rows = math.ceil(len(authors) / max(1, config.author_block.max_columns))
        columns = math.ceil(len(authors) / rows)
        table = _author_table(blocks, rows, columns, layout.frontmatter_section.column_width.twips)
        body.insert(anchor, table)
        _ensure_paragraph_follows(table)
        outcome.author_layout, outcome.author_rows, outcome.author_columns = "columns", rows, columns
        return
    for offset, paragraph in enumerate(paragraph for block in blocks for paragraph in block):
        body.insert(anchor + offset, paragraph)
    outcome.author_layout, outcome.author_rows, outcome.author_columns = "stacked", len(authors), 1


def _insert_label(paragraph: etree._Element, block: LabelledBlock, run_style: str) -> None:
    """Insert the run-in label after ``w:pPr`` (before every existing run, bookmark and math node)."""
    index = 1 if paragraph.find(w("pPr")) is not None else 0
    paragraph.insert(index, _text_run(block.run_in_text, run_style))


def _retitle(paragraph: etree._Element, label: str) -> None:
    """Replace the visible content of a title paragraph by one ``label`` run, keeping ``pPr`` and bookmarks."""
    for child in list(paragraph):
        if child.tag not in (w("pPr"), w("bookmarkStart"), w("bookmarkEnd")):
            paragraph.remove(child)
    paragraph.append(_text_run(label))


def _heading_paragraph(label: str) -> etree._Element:
    paragraph = _paragraph(styles.ABSTRACT_TITLE)
    paragraph.append(_text_run(label))
    return paragraph


def _apply_abstract(body: etree._Element, block: LabelledBlock, outcome: FrontmatterOutcome) -> None:
    abstracts = _styled(body, styles.ABSTRACT)
    outcome.abstract_layout = block.layout
    outcome.abstract_present = bool(abstracts)
    if not abstracts:
        return
    titles = _styled(body, styles.ABSTRACT_TITLE)
    if block.layout == "run-in":
        first = abstracts[0]
        _insert_label(first, block, styles.ABSTRACT_LABEL)
        for title in titles:
            anchors = [c for c in title if c.tag in (w("bookmarkStart"), w("bookmarkEnd"))]
            for position, bookmark in enumerate(anchors):
                first.insert((1 if first.find(w("pPr")) is not None else 0) + position, bookmark)
            body.remove(title)
    elif titles:
        _retitle(titles[0], block.label)
    else:
        abstracts[0].addprevious(_heading_paragraph(block.label))


def _apply_keywords(body: etree._Element, block: LabelledBlock, outcome: FrontmatterOutcome) -> None:
    keywords = _styled(body, styles.INDEX_TERMS)
    outcome.keywords_layout = block.layout
    outcome.keywords_present = bool(keywords)
    if not keywords:
        return
    if block.layout == "run-in":
        _insert_label(keywords[0], block, styles.KEYWORDS_LABEL)
    else:
        keywords[0].addprevious(_heading_paragraph(block.label))


def apply_frontmatter(
    document: etree._Element, metadata: DocumentMetadata, layout: ResolvedLayout, config: FrontmatterConfig
) -> tuple[FrontmatterOutcome, list[Finding]]:
    """Render the front matter into ``document`` (mutated in place); returns what was done plus diagnostics."""
    outcome = FrontmatterOutcome()
    findings: list[Finding] = []
    body = document.find(w("body"))
    if body is None:
        return outcome, findings
    _remove_metadata_paragraphs(body, metadata, config, outcome, findings)
    titles = _styled(body, styles.TITLE)
    outcome.title_present = bool(titles)
    if titles:
        outcome.title_text = text_of(titles[0]).strip()
        anchor = body.index(titles[0]) + 1
    else:
        findings.append(Finding("L2W-W-LAYOUT-040", Severity.WARNING, "The document has no title paragraph."))
        anchor = 0
    _insert_author_block(body, anchor, metadata, layout, config, outcome)
    _apply_abstract(body, config.abstract, outcome)
    _apply_keywords(body, config.keywords, outcome)
    return outcome, findings


# -- document properties -------------------------------------------------------------------------------


def _core_child(root: etree._Element, name: str) -> etree._Element:
    node = root.find(qn(name))
    return node if node is not None else etree.SubElement(root, qn(name))


def apply_core_properties(package: OpcPackage, metadata: DocumentMetadata) -> None:
    """Author names into ``dc:creator`` and the title into an empty ``dc:title``; timestamps stay untouched."""
    if not package.has(_CORE_PART):
        return
    root = package.xml(_CORE_PART)
    if metadata.authors:
        _core_child(root, "dc:creator").text = "; ".join(author.name for author in metadata.authors)
    if metadata.title:
        title = _core_child(root, "dc:title")
        if not (title.text or "").strip():
            title.text = metadata.title


# -- validator helper ----------------------------------------------------------------------------------


def _paragraph_lines(container: etree._Element) -> str:
    return "\n".join(text_of(p) for p in container.findall(w("p")) if text_of(p).strip())


def _table_style(table: etree._Element) -> str:
    node = table.find(f"{w('tblPr')}/{w('tblStyle')}")
    return node.get(w("val"), "") if node is not None else ""


def rendered_front_text(document: etree._Element) -> dict[str, Any]:
    """Per-author text found in the body: ``{"layout": columns|stacked|none, "authors": [str, ...]}``.

    Columns: one string per non-empty ``AuthorBlock`` cell (row-major). Stacked: each ``Author`` paragraph opens
    an author that collects the ``AuthorAffiliation``/``AuthorEmail`` paragraphs following it.
    """
    body = document.find(w("body"))
    if body is None:
        return {"layout": "none", "authors": []}
    tables = [t for t in body.findall(w("tbl")) if _table_style(t) == styles.TABLE_AUTHOR_BLOCK]
    if tables:
        cells = [_paragraph_lines(c) for t in tables for c in t.iter(w("tc"))]
        return {"layout": "columns", "authors": [text for text in cells if text]}
    authors: list[list[str]] = []
    for child in body.findall(w("p")):
        style = paragraph_style(child)
        if style == styles.AUTHOR:
            authors.append([text_of(child)])
        elif style in (styles.AUTHOR_AFFILIATION, styles.AUTHOR_EMAIL) and authors:
            authors[-1].append(text_of(child))
    return {"layout": "stacked" if authors else "none", "authors": ["\n".join(parts) for parts in authors]}
