"""Note blocks of figures and tables: the paragraph style "Caption Note" follows the source declarations.

The staging rewrite (``structure_rewrite.plan_note_rewrites``) marks each note block; the semantic filter gives its
paragraphs the style ``CaptionNote`` and puts a private bookmark ``_l2wnote_<size>_<align>_<indent>_<skip>_<f|o>_<n>``
in the first one (``f``: inside a float, ``o``: after a table outside floats). This pass:

* builds the style from the source semantics: the size of the LaTeX size command, that size's line height (LaTeX's
  ``\\baselineskip``: single-spaced inside floats; outside them scaled by the document's line stretch, i.e. the
  body line height over the normal baselineskip), the alignment and the zero paragraph indent
  of ``\\raggedright``/``\\centering``/``\\raggedleft``/``\\noindent``, the skip before (``\\medskip``...), nothing
  after. Every property the profile role ``note`` sets wins;
* gives a note whose declarations differ from the style's those properties as direct formatting;
* keeps a figure caption and its note together, as in a float: the space between them is the note's skip only,
  and the caption's space after moves below the note (the float keeps its distance to the text);
* removes the bookmarks.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import fields, replace

from lxml import etree

from latex2word.layout import ResolvedLayout, TextStyle, latex_baselineskip
from latex2word.ooxml import style_ids as ids
from latex2word.ooxml.namespaces import w
from latex2word.ooxml.styles import apply_direct_spec, apply_style_spec, style_spacing_after
from latex2word.ooxml.xmlutil import PPR_ORDER, ensure_child, paragraph_style, ppr, set_child, set_paragraph_style
from latex2word.units import Length

MARKER = re.compile(r"^_l2wnote_([A-Za-z]+)_([lcrj])_([ni])_(\d+)_([fo])_\d+$")
_ALIGNMENT = {"l": "left", "c": "center", "r": "right"}
_CAPTIONS = (ids.IMAGE_CAPTION, ids.TABLE_CAPTION)


def _body_stretch(layout: ResolvedLayout) -> float:
    """The document's line stretch (``\\baselinestretch``): the body line height over LaTeX's normal baselineskip."""
    normal = latex_baselineskip(layout.typography.get("default"), "normalsize")
    height = layout.paragraph.line_height
    return height.pt / normal if height is not None and normal else 1.0


def _semantic_spec(layout: ResolvedLayout, size: str, align: str, indent: str, skip: int, context: str) -> TextStyle:
    default = layout.typography.get("default")
    size_pt = layout.font_sizes.get(size)
    baseline = latex_baselineskip(default, size)
    if baseline is not None and context == "o":
        # floats are typeset single-spaced (setspace); outside them the document's line stretch applies
        baseline = round(baseline * _body_stretch(layout), 1)
    return TextStyle(
        size_pt=size_pt,
        alignment=_ALIGNMENT.get(align),
        indent_first_line=Length(0) if indent == "n" else None,
        before_pt=skip / 20,
        after_pt=0,
        line_height=Length.points(baseline) if baseline is not None else None,
        line_rule="atLeast" if baseline is not None else None,
    )


def _with_profile(spec: TextStyle, layout: ResolvedLayout) -> TextStyle:
    role = layout.typography.get("note")
    if role is None:
        return spec
    overrides = {f.name: getattr(role, f.name) for f in fields(TextStyle) if getattr(role, f.name) is not None}
    if "indent_hanging" in overrides:
        overrides["indent_first_line"] = None
    return replace(spec, **overrides)


def _block(first: etree._Element) -> list[etree._Element]:
    """The marked paragraph and the Caption Note paragraphs that follow it."""
    block = [first]
    nxt = first.getnext()
    while nxt is not None and nxt.tag == w("p") and paragraph_style(nxt) == ids.CAPTION_NOTE and not any(
        MARKER.match(b.get(w("name"), "")) for b in nxt.iter(w("bookmarkStart"))
    ):
        block.append(nxt)
        nxt = nxt.getnext()
    return block


def _spacing(paragraph: etree._Element) -> etree._Element:
    return ensure_child(ppr(paragraph), "spacing", PPR_ORDER)


def apply_caption_notes(document: etree._Element, styles: etree._Element, layout: ResolvedLayout) -> int:
    """Style the marked note blocks (see the module docstring); returns how many there are."""
    found: list[tuple[tuple[str, str, str, int, str], etree._Element]] = []
    for mark in list(document.iter(w("bookmarkStart"))):
        match = MARKER.match(mark.get(w("name"), ""))
        if match is None:
            continue
        paragraph = next((a for a in mark.iterancestors() if a.tag == w("p")), None)
        ident = mark.get(w("id"))
        for end in list(document.iter(w("bookmarkEnd"))):
            if end.get(w("id")) == ident:
                end.getparent().remove(end)
        mark.getparent().remove(mark)
        if paragraph is not None:
            size, align, indent, skip, context = match.groups()
            found.append(((size, align, indent, int(skip), context), paragraph))
    if not found:
        return 0

    common = Counter(key for key, _ in found).most_common(1)[0][0]
    apply_style_spec(styles, ids.CAPTION_NOTE, _with_profile(_semantic_spec(layout, *common), layout))
    for key, first in found:
        block = _block(first)
        if key != common:
            spec = _with_profile(_semantic_spec(layout, *key), layout)
            for paragraph in block:
                apply_direct_spec(paragraph, spec)
        # a multi-paragraph note stays in one piece
        for paragraph in block[:-1]:
            set_child(ppr(paragraph), "keepNext", PPR_ORDER)
        # Pandoc starts a "first paragraph" after the note's Div; in LaTeX the text after a float or a table is an
        # ordinary paragraph, as it was without the note block
        following = block[-1].getnext()
        if following is not None and following.tag == w("p") and paragraph_style(following) == ids.FIRST_PARAGRAPH:
            set_paragraph_style(following, ids.BODY_TEXT)
        previous = first.getprevious()
        if previous is not None and previous.tag == w("p") and paragraph_style(previous) in _CAPTIONS:
            caption_after = style_spacing_after(styles, paragraph_style(previous) or "")
            direct = previous.find(f"{w('pPr')}/{w('spacing')}")
            if direct is not None and direct.get(w("after")) is not None:
                caption_after = int(direct.get(w("after")))
            # caption, note and the rest of the float stay together; the float's gap to the text follows the note
            set_child(ppr(previous), "keepNext", PPR_ORDER)
            _spacing(previous).set(w("after"), "0")
            last = _spacing(block[-1])
            note_after = int(last.get(w("after"))) if last.get(w("after")) is not None else style_spacing_after(
                styles, ids.CAPTION_NOTE
            )
            last.set(w("after"), str(max(note_after, caption_after)))
    return len(found)


__all__ = ["MARKER", "apply_caption_notes"]
