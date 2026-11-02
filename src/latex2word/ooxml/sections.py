"""Section geometry: where the body section starts, and which geometry applies to an element.

The front matter and the body are two Word sections only when their column
counts differ (``ResolvedLayout.needs_section_break``); the transition is a
*continuous* section break carried by the final paragraph of the front matter
(``w:sectPr`` in its ``w:pPr``), and the document-level ``w:sectPr`` describes
the body section. No marker text is ever inserted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lxml import etree

from latex2word.layout import ResolvedLayout, SectionGeometry
from latex2word.ooxml.namespaces import qn, text_of, w
from latex2word.ooxml.style_ids import HEADING
from latex2word.ooxml.xmlutil import PPR_ORDER, SECTPR_ORDER, ensure_child, paragraph_style, ppr


@dataclass
class SectionPlan:
    """Which part of the body belongs to which section."""

    layout: ResolvedLayout
    body: etree._Element  # the w:body element
    body_start: etree._Element | None  # first body-level element of the body section (None: one section only)

    def _top_index(self, element: etree._Element) -> int | None:
        node: etree._Element | None = element
        while node is not None and node.getparent() is not self.body:
            node = node.getparent()
        return None if node is None else self.body.index(node)

    def is_frontmatter(self, element: etree._Element) -> bool:
        if self.body_start is None:
            return False
        index, start = self._top_index(element), self.body.index(self.body_start)
        return index is not None and index < start

    def geometry_for(self, element: etree._Element) -> SectionGeometry:
        return self.layout.frontmatter_section if self.is_frontmatter(element) else self.layout.body_section


def _sect_pr(layout: ResolvedLayout, columns: int, *, continuous: bool) -> etree._Element:
    page = layout.page
    sect = etree.Element(w("sectPr"))
    if continuous:
        etree.SubElement(sect, w("type")).set(w("val"), "continuous")
    size = etree.SubElement(sect, w("pgSz"))
    size.set(w("w"), str(page.width.twips))
    size.set(w("h"), str(page.height.twips))
    size.set(w("orient"), page.orientation)
    margin = etree.SubElement(sect, w("pgMar"))
    for key, value in (
        ("top", page.top), ("right", page.right), ("bottom", page.bottom), ("left", page.left),
        ("header", page.header_distance), ("footer", page.footer_distance),
    ):
        margin.set(w(key), str(value.twips))
    margin.set(w("gutter"), "0")
    cols = etree.SubElement(sect, w("cols"))
    cols.set(w("num"), str(columns))
    cols.set(w("space"), str(layout.column_gap.twips))
    cols.set(w("equalWidth"), "1")
    return sect


def body_section_properties(layout: ResolvedLayout) -> etree._Element:
    """``w:sectPr`` of the body section (document level). Continuous after a front-matter section."""
    return _sect_pr(layout, layout.body_columns, continuous=layout.needs_section_break)


def frontmatter_section_properties(layout: ResolvedLayout) -> etree._Element:
    """``w:sectPr`` that ends the front-matter section (placed in the last front-matter paragraph)."""
    return _sect_pr(layout, layout.frontmatter_columns, continuous=False)


def final_sect_pr(body: etree._Element) -> etree._Element | None:
    last = body[-1] if len(body) else None
    return last if last is not None and last.tag == qn("w:sectPr") else None


def reference_section_properties(layout: ResolvedLayout) -> etree._Element:
    """``w:sectPr`` of a reference document: the page geometry Pandoc copies into its output."""
    return _sect_pr(layout, layout.body_columns, continuous=False)


def _heading_level(paragraph: etree._Element) -> int | None:
    style = paragraph_style(paragraph)
    if style in HEADING:
        return HEADING.index(style) + 1
    return None


def find_body_start(
    body: etree._Element, strategy: str, numbered_sections: Sequence[Mapping[str, Any]]
) -> etree._Element | None:
    """The body-level heading paragraph where the body section begins, derived from the document structure.

    ``first-numbered-heading``: the first level-1 heading that carries the semantic number the filter assigned to
    the first numbered level-1 section (its ``text``, e.g. ``"I."``, and its ``title``); unnumbered level-1 headings
    before it do not count. ``first-heading``: the first level-1 heading. No heading text is ever hard-coded.
    """
    first_numbered = next((s for s in numbered_sections if s.get("level") == 1), None)
    for child in body:
        if child.tag != qn("w:p") or _heading_level(child) != 1:
            continue
        if strategy == "first-heading":
            return child
        if strategy == "first-numbered-heading" and first_numbered is not None:
            text = " ".join(text_of(child).split())
            if text.startswith(str(first_numbered.get("text", "\0"))) and str(first_numbered.get("title") or "") in text:
                return child
    return None


def last_frontmatter_paragraph(body: etree._Element, body_start: etree._Element) -> etree._Element:
    """The paragraph that ends the front-matter section (carries its ``w:sectPr``).

    It is the nearest body-level paragraph before ``body_start``; when a table (not a paragraph) is the last
    front-matter block, an empty paragraph is added after it, because a section break lives in a paragraph.
    """
    node = body_start.getprevious()
    while node is not None and node.tag in (qn("w:bookmarkStart"), qn("w:bookmarkEnd")):
        node = node.getprevious()
    if node is not None and node.tag == qn("w:p"):
        return node
    carrier = etree.Element(w("p"))
    body_start.addprevious(carrier)
    return carrier


def apply_section_properties(body: etree._Element, layout: ResolvedLayout, body_start: etree._Element | None) -> int:
    """Write the page geometry/columns into the final ``w:sectPr`` and, when needed, the front-matter section break.

    Returns the number of sections written. The existing final ``w:sectPr`` keeps its non-geometry children
    (header/footer references, footnote properties, ...); only type/pgSz/pgMar/cols are replaced.
    """
    final = final_sect_pr(body)
    if final is None:
        final = etree.SubElement(body, w("sectPr"))
    wanted = body_section_properties(layout)
    for name in ("type", "pgSz", "pgMar", "cols"):
        for old in final.findall(w(name)):
            final.remove(old)
        new = wanted.find(w(name))
        if new is not None:
            created = ensure_child(final, name, SECTPR_ORDER)
            created.attrib.update(new.attrib)
    sections = 1
    if layout.needs_section_break:
        if body_start is None:
            raise ValueError("a front-matter section break needs a body start element")
        carrier = last_frontmatter_paragraph(body, body_start)
        container = ppr(carrier)
        for old in container.findall(w("sectPr")):
            container.remove(old)
        ensure_child(container, "sectPr", PPR_ORDER).extend(list(frontmatter_section_properties(layout)))
        sections = 2
    return sections
