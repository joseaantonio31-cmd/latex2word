"""Word sections and page numbers.

The semantic filter writes an empty paragraph with a private bookmark
``_l2wpn_<format|keep>_<start|keep>`` where the source changes its page
numbering (``\\pagenumbering{...}``, ``\\setcounter{page}{n}``). This pass turns
every such paragraph into a next-page section break:

* the marker paragraph becomes the last paragraph of the section before it and
  carries that section's ``w:sectPr`` (geometry copied from the document-level
  ``w:sectPr``) with its ``w:pgNumType``;
* the document-level ``w:sectPr`` describes the last section;
* a marker with nothing visible before it (``\\pagenumbering{gobble}`` at the
  start of the document) only sets the numbering of the first section;
* every section references a header/footer part with a ``PAGE`` field
  (``layout.pagination.page_numbers``), or an empty one where the source asks
  for no number (``gobble``), so nothing is inherited by accident;
* the first paragraph of a new section gets ``w:pageBreakBefore=0`` because the
  section break already starts the page (a heading style that breaks the page
  would otherwise leave a blank page).

Page numbers are Word fields, never literal text; TOC fields therefore show the
logical numbers (i, ii, ..., 1, 2, ...).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from latex2word.layout import PaginationLayout
from latex2word.ooxml.namespaces import NS, w
from latex2word.ooxml.package import CONTENT_TYPES, OpcPackage
from latex2word.ooxml.xmlutil import PPR_ORDER, SECTPR_ORDER, ensure_child, ppr, set_child

MARKER = re.compile(r"^_l2wpn_(?P<format>[A-Za-z]+)_(?P<start>\d+|keep)$")
WORD_FORMATS = frozenset({"decimal", "lowerRoman", "upperRoman", "lowerLetter", "upperLetter", "none"})
_R = NS["r"]
_PKG = NS["pkg"]
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/{part}"
_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.{part}+xml"
_GEOMETRY = ("pgSz", "pgMar", "cols")
_VISIBLE = (w("t"), w("drawing"), w("pict"), w("object"), "{%s}oMath" % NS["m"], w("tbl"))


@dataclass
class SectionSpec:
    format: str  # Word ST_NumberFormat or "none" (no visible number)
    start: int | None  # None: continue the previous section's count


@dataclass
class PaginationOutcome:
    sections: list[dict[str, Any]] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    markers: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"sections": self.sections, "parts": self.parts, "markers": self.markers}


def _marker_of(paragraph: etree._Element) -> re.Match[str] | None:
    for mark in paragraph.findall(w("bookmarkStart")):
        match = MARKER.match(mark.get(w("name"), ""))
        if match:
            return match
    return None


def _has_visible(element: etree._Element) -> bool:
    if element.tag in _VISIBLE:
        return True
    for tag in _VISIBLE:
        for node in element.iter(tag):
            if tag != w("t") or (node.text or "").strip():
                return True
    return False


def _field_paragraph(alignment: str) -> etree._Element:
    p = etree.Element(w("p"))
    set_child(ppr(p), "jc", PPR_ORDER, val={"left": "left", "right": "right"}.get(alignment, "center"))
    for kind in ("begin", None, "separate", "text", "end"):
        run = etree.SubElement(p, w("r"))
        if kind == "begin":
            etree.SubElement(run, w("fldChar")).set(w("fldCharType"), "begin")
        elif kind is None:
            instr = etree.SubElement(run, w("instrText"))
            instr.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            instr.text = " PAGE "
        elif kind == "text":
            etree.SubElement(run, w("t")).text = "1"
        else:
            etree.SubElement(run, w("fldChar")).set(w("fldCharType"), kind)
    return p


def _part_xml(part: str, paragraph: etree._Element) -> bytes:
    root = etree.Element(w("hdr" if part == "header" else "ftr"), nsmap={"w": NS["w"], "r": _R})
    root.append(paragraph)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _add_part(package: OpcPackage, part: str, name: str, data: bytes, rel_id: str) -> None:
    package.set_bytes(f"word/{name}", data)
    types = package.xml(CONTENT_TYPES)
    if not any(o.get("PartName") == f"/word/{name}" for o in types.iter(f"{{{_CT}}}Override")):
        override = etree.SubElement(types, f"{{{_CT}}}Override")
        override.set("PartName", f"/word/{name}")
        override.set("ContentType", _CONTENT_TYPE.format(part=part))
    rels = package.xml("word/_rels/document.xml.rels")
    if not any(r.get("Id") == rel_id for r in rels.iter(f"{{{_PKG}}}Relationship")):
        rel = etree.SubElement(rels, f"{{{_PKG}}}Relationship")
        rel.set("Id", rel_id)
        rel.set("Type", _REL_TYPE.format(part=part))
        rel.set("Target", name)


def _spec_of(match: re.Match[str], previous: SectionSpec) -> SectionSpec:
    fmt = match.group("format")
    start = match.group("start")
    return SectionSpec(previous.format if fmt == "keep" else fmt, None if start == "keep" else int(start))


def _write_numbering(sect: etree._Element, spec: SectionSpec, refs: dict[str, str], *, first_page_empty: bool) -> None:
    for old in sect.findall(w("pgNumType")) + sect.findall(w("headerReference")) + sect.findall(w("footerReference")) \
            + sect.findall(w("titlePg")):
        sect.remove(old)
    number = ensure_child(sect, "pgNumType", SECTPR_ORDER)
    number.set(w("fmt"), "decimal" if spec.format == "none" else spec.format)
    if spec.start is not None:
        number.set(w("start"), str(spec.start))
    if refs:
        part = next(iter(refs)).split(":")[0]
        reference = etree.Element(w(f"{part}Reference"))
        reference.set(w("type"), "default")
        reference.set(f"{{{_R}}}id", refs[f"{part}:{'empty' if spec.format == 'none' else 'page'}"])
        sect.insert(0, reference)
        if first_page_empty and spec.format != "none":
            first = etree.Element(w(f"{part}Reference"))
            first.set(w("type"), "first")
            first.set(f"{{{_R}}}id", refs[f"{part}:empty"])
            sect.insert(1, first)
            ensure_child(sect, "titlePg", SECTPR_ORDER)


def apply_pagination(package: OpcPackage, document: etree._Element, pagination: PaginationLayout) -> PaginationOutcome:
    """Turn pagination markers into sections, write ``w:pgNumType`` and the PAGE header/footer parts."""
    outcome = PaginationOutcome()
    body = document.find(w("body"))
    final = body.find(w("sectPr")) if body is not None else None
    if body is None or final is None:
        return outcome
    markers = [(p, m) for p in body.findall(w("p")) if (m := _marker_of(p)) is not None]
    outcome.markers = len(markers)
    # stray markers outside the body's top level (e.g. in a table cell) cannot end a section: drop them
    for mark in list(body.iter(w("bookmarkStart"))):
        if MARKER.match(mark.get(w("name"), "")) and mark.getparent() not in [p for p, _ in markers]:
            _drop_bookmark(mark)
    if not markers and pagination.page_numbers == "none":
        return outcome

    refs: dict[str, str] = {}
    part = pagination.part
    if part is not None:
        _add_part(package, part, f"{part}_l2w_page.xml", _part_xml(part, _field_paragraph(pagination.alignment)), "rIdL2wPage")
        _add_part(package, part, f"{part}_l2w_empty.xml", _part_xml(part, etree.Element(w("p"))), "rIdL2wEmpty")
        refs = {f"{part}:page": "rIdL2wPage", f"{part}:empty": "rIdL2wEmpty"}
        outcome.parts = [f"word/{part}_l2w_page.xml", f"word/{part}_l2w_empty.xml"]

    spec = SectionSpec(pagination.format, pagination.start)
    first_numbered_done = False
    seen_visible = False
    children = list(body)
    boundaries: list[tuple[etree._Element, SectionSpec]] = []
    marker_set = {id(p): m for p, m in markers}
    for child in children:
        if id(child) in marker_set:
            new_spec = _spec_of(marker_set[id(child)], spec)
            if not seen_visible:  # nothing before it: it only sets the numbering of the first section
                spec = new_spec
                body.remove(child)
                continue
            boundaries.append((child, spec))
            spec = new_spec
            continue
        if child.tag != w("sectPr") and _has_visible(child):
            seen_visible = True
    for carrier, previous in boundaries:
        sect = etree.Element(w("sectPr"))
        for name in _GEOMETRY:
            node = final.find(w(name))
            if node is not None:
                sect.append(copy.deepcopy(node))
        suppress = pagination.suppress_first_page and not first_numbered_done and previous.format != "none"
        _write_numbering(sect, previous, refs, first_page_empty=suppress)
        first_numbered_done = first_numbered_done or previous.format != "none"
        for mark in list(carrier.findall(w("bookmarkStart"))) + list(carrier.findall(w("bookmarkEnd"))):
            carrier.remove(mark)
        ensure_child(ppr(carrier), "sectPr", PPR_ORDER).extend(list(sect))
        outcome.sections.append({"format": previous.format, "start": previous.start})
        following = carrier.getnext()
        while following is not None and following.tag != w("p"):
            following = following.getnext() if following.tag == w("bookmarkStart") else None
        if following is not None:
            set_child(ppr(following), "pageBreakBefore", PPR_ORDER, val="0")
    suppress = pagination.suppress_first_page and not first_numbered_done and spec.format != "none"
    _write_numbering(final, spec, refs, first_page_empty=suppress)
    outcome.sections.append({"format": spec.format, "start": spec.start})
    return outcome


def _drop_bookmark(mark: etree._Element) -> None:
    parent = mark.getparent()
    ident = mark.get(w("id"))
    for end in parent.findall(w("bookmarkEnd")):
        if end.get(w("id")) == ident:
            parent.remove(end)
    parent.remove(mark)


__all__ = ["MARKER", "PaginationOutcome", "SectionSpec", "apply_pagination"]
