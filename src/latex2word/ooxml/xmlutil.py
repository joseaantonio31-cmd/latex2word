"""Schema-ordered editing of WordprocessingML property containers.

Word is strict about the order of children in ``w:pPr``, ``w:rPr``, ``w:tblPr``,
``w:tcPr`` and ``w:sectPr``. :func:`set_child` and :func:`ensure_child` insert a
child at the position the schema requires, so every transform stays valid
regardless of what Pandoc (or an external template) already wrote.
"""

from __future__ import annotations

from lxml import etree

from latex2word.ooxml.namespaces import w

PPR_ORDER = (
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr", "suppressLineNumbers",
    "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct", "topLinePunct",
    "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid", "spacing", "ind", "contextualSpacing",
    "mirrorIndents", "suppressOverlap", "jc", "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl",
    "divId", "cnfStyle", "rPr", "sectPr", "pPrChange",
)
RPR_ORDER = (
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline", "shadow",
    "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern",
    "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em",
    "lang", "eastAsianLayout", "specVanish", "oMath",
)
TBLPR_ORDER = (
    "tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize", "tblStyleColBandSize", "tblW", "jc",
    "tblCellSpacing", "tblInd", "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook", "tblCaption",
    "tblDescription", "tblPrChange",
)
TRPR_ORDER = (
    "cnfStyle", "divId", "gridBefore", "gridAfter", "wBefore", "wAfter", "cantSplit", "trHeight", "tblHeader",
    "tblCellSpacing", "jc", "hidden", "ins", "del", "trPrChange",
)
TCPR_ORDER = (
    "cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar", "textDirection",
    "tcFitText", "vAlign", "hideMark", "headers", "cellIns", "cellDel", "cellMerge", "tcPrChange",
)
SECTPR_ORDER = (
    "headerReference", "footerReference", "footnotePr", "endnotePr", "type", "pgSz", "pgMar", "paperSrc",
    "pgBorders", "lnNumType", "pgNumType", "cols", "formProt", "vAlign", "noEndnote", "titlePg", "textDirection",
    "bidi", "rtlGutter", "docGrid", "printerSettings", "sectPrChange",
)
STYLE_ORDER = (
    "name", "aliases", "basedOn", "next", "link", "autoRedefine", "hidden", "uiPriority", "semiHidden",
    "unhideWhenUsed", "qFormat", "locked", "personal", "personalCompose", "personalReply", "rsid", "pPr", "rPr",
    "tblPr", "trPr", "tcPr", "tblStylePr",
)


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def ensure_child(parent: etree._Element, name: str, order: tuple[str, ...]) -> etree._Element:
    """The child ``w:<name>`` of ``parent`` (created at its schema position when missing)."""
    existing = parent.find(w(name))
    if existing is not None:
        return existing
    child = etree.Element(w(name))
    rank = order.index(name)
    for index, sibling in enumerate(parent):
        local = _local(sibling.tag)
        if local in order and order.index(local) > rank:
            parent.insert(index, child)
            return child
    parent.append(child)
    return child


def set_child(parent: etree._Element, name: str, order: tuple[str, ...], **attributes: str) -> etree._Element:
    """Create-or-replace ``w:<name>`` with exactly ``attributes`` (names without the ``w:`` prefix)."""
    child = ensure_child(parent, name, order)
    for key in list(child.attrib):
        del child.attrib[key]
    for key, value in attributes.items():
        child.set(w(key), value)
    return child


def remove_child(parent: etree._Element, name: str) -> None:
    for child in parent.findall(w(name)):
        parent.remove(child)


def ppr(paragraph: etree._Element) -> etree._Element:
    """``w:pPr`` of a paragraph (created as its first child when missing)."""
    existing = paragraph.find(w("pPr"))
    if existing is not None:
        return existing
    created = etree.Element(w("pPr"))
    paragraph.insert(0, created)
    return created


def paragraph_style(paragraph: etree._Element) -> str:
    node = paragraph.find(f"{w('pPr')}/{w('pStyle')}")
    return node.get(w("val"), "") if node is not None else ""


def set_paragraph_style(paragraph: etree._Element, style_id: str) -> None:
    set_child(ppr(paragraph), "pStyle", PPR_ORDER, val=style_id)
