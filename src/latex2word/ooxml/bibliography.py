"""Bibliography paragraphs: real tab characters and style-owned indentation.

The text of the entries and their order belong to the semantic stage (citeproc / manual mode) and are never
touched. Two layout facts are normalised here:

* a label separator written as a literal TAB character inside ``w:t`` becomes a real ``w:tab`` run, which is
  what makes a hanging indent (label, tab, text) align in Word;
* direct ``w:ind``/``w:spacing`` on a ``Bibliography`` paragraph (some CSL styles make Pandoc write them) would
  override the profile's bibliography style, so they are removed: the named style owns that formatting.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from lxml import etree

from latex2word.ooxml import style_ids
from latex2word.ooxml.namespaces import w
from latex2word.ooxml.xmlutil import paragraph_style


@dataclass(frozen=True)
class BibliographyOutcome:
    entries: int
    tabs_converted: int
    direct_formats_removed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": self.entries, "tabs_converted": self.tabs_converted,
            "direct_formats_removed": self.direct_formats_removed,
        }


def _split_run(run: etree._Element, text: etree._Element) -> int:
    """Replace ``run`` (whose only text node holds TABs) by runs of text and ``w:tab`` runs; returns tabs made."""
    pieces = (text.text or "").split("\t")
    properties = run.find(w("rPr"))
    parent = run.getparent()
    position = parent.index(run)
    made = 0
    for index, piece in enumerate(pieces):
        if piece:
            created = etree.Element(w("r"))
            if properties is not None:
                created.append(copy.deepcopy(properties))
            node = etree.SubElement(created, w("t"))
            node.text = piece
            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            parent.insert(position, created)
            position += 1
        if index < len(pieces) - 1:
            created = etree.Element(w("r"))
            if properties is not None:
                created.append(copy.deepcopy(properties))
            etree.SubElement(created, w("tab"))
            parent.insert(position, created)
            position += 1
            made += 1
    parent.remove(run)
    return made


def apply_bibliography_layout(document: etree._Element) -> BibliographyOutcome:
    entries = tabs = removed = 0
    for paragraph in document.iter(w("p")):
        if paragraph_style(paragraph) != style_ids.BIBLIOGRAPHY:
            continue
        entries += 1
        properties = paragraph.find(w("pPr"))
        if properties is not None:
            for name in ("ind", "spacing"):
                for node in properties.findall(w(name)):
                    properties.remove(node)
                    removed += 1
        for run in list(paragraph.findall(w("r"))):
            texts = run.findall(w("t"))
            if len(texts) == 1 and len(run) - (1 if run.find(w("rPr")) is not None else 0) == 1 and "\t" in (texts[0].text or ""):
                tabs += _split_run(run, texts[0])
    return BibliographyOutcome(entries, tabs, removed)
