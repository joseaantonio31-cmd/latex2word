"""Unlisted headings: starred LaTeX headings (\\chapter*, \\section*, ...) are not TOC entries.

The semantic filter marks such a heading with a private bookmark ``_l2wunlisted_<n>``.
This pass gives the paragraph the style ``Heading<n>Unlisted``: based on ``Heading<n>``
(identical look, page breaks, keep-with-next) but with outline level 9 ("body text"),
so Word's TOC field (``\\o`` built-in heading styles, ``\\u`` outline levels) does not
list it. An explicit ``\\addcontentsline{toc}`` in the source adds the entry back as a
TC field, exactly as in LaTeX.
"""

from __future__ import annotations

import re

from lxml import etree

from latex2word.ooxml import style_ids as ids
from latex2word.ooxml.namespaces import w
from latex2word.ooxml.xmlutil import PPR_ORDER, STYLE_ORDER, ensure_child, paragraph_style, ppr, set_child, set_paragraph_style

MARKER = re.compile(r"^_l2wunlisted_\d+$")
_HEADING = re.compile(r"^Heading([1-9])$")


def unlisted_style_id(heading_style: str) -> str:
    return heading_style + ids.UNLISTED_SUFFIX


def _ensure_unlisted_style(styles: etree._Element, heading_style: str) -> str:
    style_id = unlisted_style_id(heading_style)
    for style in styles.iter(w("style")):
        if style.get(w("styleId")) == style_id:
            return style_id
    level = _HEADING.match(heading_style).group(1)  # type: ignore[union-attr]
    style = etree.SubElement(styles, w("style"))
    style.set(w("type"), "paragraph")
    style.set(w("customStyle"), "1")
    style.set(w("styleId"), style_id)
    set_child(style, "name", STYLE_ORDER, val=f"Heading {level} Unlisted")
    set_child(style, "basedOn", STYLE_ORDER, val=heading_style)
    set_child(style, "next", STYLE_ORDER, val=ids.BODY_TEXT)
    set_child(style, "qFormat", STYLE_ORDER)
    set_child(ensure_child(style, "pPr", STYLE_ORDER), "outlineLvl", PPR_ORDER, val="9")
    return style_id


def apply_unlisted_headings(document: etree._Element, styles: etree._Element) -> int:
    """Restyle every marked heading; returns how many were changed. Markers are always removed.

    Only a document with a TOC field built from headings is restyled: an outline level of body text also
    hides the heading from Word's navigation pane, which is worth it only where a TOC would list it.
    """
    has_toc = any(
        (node.text or "").strip().startswith("TOC") and "\\o" in (node.text or "")
        for node in document.iter(w("instrText"))
    )
    changed = 0
    for mark in list(document.iter(w("bookmarkStart"))):
        if not MARKER.match(mark.get(w("name"), "")):
            continue
        paragraph = next((a for a in mark.iterancestors() if a.tag == w("p")), None)
        ident = mark.get(w("id"))
        for end in list(document.iter(w("bookmarkEnd"))):
            if end.get(w("id")) == ident:
                end.getparent().remove(end)
        mark.getparent().remove(mark)
        if paragraph is None:
            continue
        style = paragraph_style(paragraph)
        if has_toc and _HEADING.match(style or ""):
            set_paragraph_style(paragraph, _ensure_unlisted_style(styles, style))
            changed += 1
    return changed


_RULE_MARKER = re.compile(r"^_l2wrule_\d+$")


def unindent_after_rules(document: etree._Element) -> int:
    """The paragraph after a rule paragraph (marked ``_l2wrule_<n>``) gets no first-line indent.

    In LaTeX, ``\\hrulefill\\\\ Name\\\\ Role`` is one paragraph: only the rule line carries the paragraph
    indent, and the text after the line break starts at the margin. Markers are always removed.
    """
    changed = 0
    for mark in list(document.iter(w("bookmarkStart"))):
        if not _RULE_MARKER.match(mark.get(w("name"), "")):
            continue
        paragraph = mark.getparent()
        ident = mark.get(w("id"))
        for end in list(paragraph.iter(w("bookmarkEnd"))):
            if end.get(w("id")) == ident:
                paragraph.remove(end)
        paragraph.remove(mark)
        following = paragraph.getnext()
        if following is not None and following.tag == w("p"):
            indent = ensure_child(ppr(following), "ind", PPR_ORDER)
            indent.attrib.pop(w("hanging"), None)
            indent.set(w("firstLine"), "0")
            changed += 1
    return changed


__all__ = ["apply_unlisted_headings", "unindent_after_rules", "unlisted_style_id"]
