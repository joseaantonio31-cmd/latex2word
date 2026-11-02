"""``word/styles.xml``: apply the profile typography, define layout table styles, describe the result.

Two entry points serve the two reference-document modes:

* :func:`apply_typography` patches Pandoc's default styles in place. The profile
  owns the look, so Pandoc's theme fonts and its blue theme-coloured headings
  are removed rather than left to leak into a publisher layout.
* :func:`ensure_required_styles` is for externally authored templates: it only
  *adds* styles the layout code refers to and never touches an existing one.

Styles are always addressed by ``w:styleId``, never by display name, and every
property container is edited through the schema-order helpers of
:mod:`latex2word.ooxml.xmlutil`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import fields, replace
from typing import Any

from lxml import etree

from latex2word.errors import BuildError
from latex2word.layout import ResolvedLayout, TextStyle
from latex2word.ooxml import style_ids as ids
from latex2word.ooxml.namespaces import w
from latex2word.ooxml.xmlutil import (
    PPR_ORDER,
    RPR_ORDER,
    STYLE_ORDER,
    TBLPR_ORDER,
    ensure_child,
    ppr,
    set_child,
)
from latex2word.units import Length

DEFAULT_PARAGRAPH_FONT = "DefaultParagraphFont"

# styles Word knows by name: they must not be flagged ``w:customStyle`` when they have to be created
# Word built-in styles (never flagged customStyle): Word applies a custom-flagged "toc N" as a different style and
# keeps its own built-in TOC formatting when it rebuilds a TOC field
_WORD_BUILTIN = frozenset(
    {ids.NORMAL, ids.BODY_TEXT, ids.TITLE, ids.SUBTITLE, ids.DATE, ids.BIBLIOGRAPHY, *ids.HEADING, *ids.TOC_LEVELS}
)
_PARENTS = {
    ids.FIRST_PARAGRAPH: ids.BODY_TEXT, ids.COMPACT: ids.BODY_TEXT, ids.AUTHOR_AFFILIATION: ids.AUTHOR,
    ids.AUTHOR_EMAIL: ids.AUTHOR, ids.EQUATION_SPACER: ids.EQUATION, ids.CAPTIONED_FIGURE: "Figure",
    ids.CENTERED: ids.BODY_TEXT, ids.COMPACT_UNCAPTIONED: ids.COMPACT, ids.CAPTION_NOTE: ids.BODY_TEXT,
}
_NEXT = {
    **{heading: ids.BODY_TEXT for heading in ids.HEADING}, ids.TITLE: ids.BODY_TEXT, ids.ABSTRACT: ids.BODY_TEXT,
    ids.ABSTRACT_TITLE: ids.ABSTRACT, ids.FIRST_PARAGRAPH: ids.BODY_TEXT,
}
_STYLE_ROLE = {style_id: role for role, style_ids in ids.ROLE_STYLES.items() for style_id in style_ids}
_TABLE_STYLES = (ids.TABLE, ids.TABLE_EQUATION_LAYOUT, ids.TABLE_AUTHOR_BLOCK, ids.TABLE_IMAGE_LAYOUT)

# Pandoc styles whose theme fonts / theme colours must not reach a publisher layout
_THEMED = (*(f"Heading{level}" for level in range(1, 10)), "TOCHeading", ids.TITLE, ids.SUBTITLE)
_THEME_FONT_ATTRIBUTES = ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme")
_PARAGRAPH_FIELDS = (
    "alignment", "before_pt", "after_pt", "line_spacing", "line_height", "line_rule", "keep_next", "keep_lines", "widow_control", "page_break_before", "right_tab_leader",
    "indent_left", "indent_first_line", "indent_hanging",
)
_RUN_FIELDS = tuple(f.name for f in fields(TextStyle) if f.name not in _PARAGRAPH_FIELDS)
_JC = {"left": "left", "right": "right", "center": "center", "justify": "both"}
_JC_BACK = {"left": "left", "start": "left", "right": "right", "end": "right", "center": "center", "both": "justify"}

_BORDER_SIDES = ("top", "left", "bottom", "right", "insideH", "insideV")
_RULE_EIGHTHS = 8  # heavy table rule: 1 pt
_HAIRLINE_EIGHTHS = 4  # header separator / grid line: 1/2 pt
_AUTHOR_BLOCK_SIDE_MARGIN_TWIPS = 60


# --------------------------------------------------------------------------- lookup


def _find(styles: etree._Element, style_id: str) -> etree._Element | None:
    for style in styles.iterfind(w("style")):
        if style.get(w("styleId")) == style_id:
            return style
    return None


def style_ids_in(styles: etree._Element) -> set[str]:
    """Every ``w:styleId`` defined in the styles part."""
    return {sid for style in styles.iterfind(w("style")) if (sid := style.get(w("styleId"))) is not None}


def _half_up(value: float) -> int:
    """Round half away from zero (Python's ``round`` is banker's rounding, which would make 9.25 pt -> 18 half-points)."""
    return int(math.floor(value + 0.5))


def _twips_of_pt(points: float) -> str:
    return str(_half_up(points * 20))


# --------------------------------------------------------------------------- TextStyle -> properties


def _toggle(parent: etree._Element, names: tuple[str, ...], order: tuple[str, ...], value: bool) -> None:
    """An on/off property; ``False`` is written explicitly so it overrides an inherited ``True``."""
    for name in names:
        if value:
            set_child(parent, name, order)
        else:
            set_child(parent, name, order, val="0")


def _strip_theme_fonts(rpr: etree._Element) -> None:
    fonts = rpr.find(w("rFonts"))
    if fonts is None:
        return
    for attribute in _THEME_FONT_ATTRIBUTES:
        fonts.attrib.pop(w(attribute), None)
    if not fonts.attrib:
        rpr.remove(fonts)


def _apply_run_properties(rpr: etree._Element, style: TextStyle) -> None:
    if style.font is not None:
        set_child(rpr, "rFonts", RPR_ORDER, ascii=style.font, hAnsi=style.font, cs=style.font, eastAsia=style.font)
    if style.bold is not None:
        _toggle(rpr, ("b", "bCs"), RPR_ORDER, style.bold)
    if style.italic is not None:
        _toggle(rpr, ("i", "iCs"), RPR_ORDER, style.italic)
    if style.caps is not None:
        _toggle(rpr, ("caps",), RPR_ORDER, style.caps == "all")
        _toggle(rpr, ("smallCaps",), RPR_ORDER, style.caps == "small")
    if style.color is not None:
        set_child(rpr, "color", RPR_ORDER, val=style.color)
    if style.size_pt is not None:
        half_points = str(_half_up(style.size_pt * 2))
        set_child(rpr, "sz", RPR_ORDER, val=half_points)
        set_child(rpr, "szCs", RPR_ORDER, val=half_points)


def _set_spacing(
    ppr_element: etree._Element, *, before: float | None = None, after: float | None = None, line: float | None = None,
    height: Length | None = None, rule: str | None = None,
) -> None:
    """Merge into ``w:spacing``: only the given attributes change; auto-spacing would override twips, so it goes."""
    spacing = ensure_child(ppr_element, "spacing", PPR_ORDER)
    for value, key in ((before, "before"), (after, "after")):
        if value is not None:
            spacing.set(w(key), _twips_of_pt(value))
            for stale in (f"{key}Autospacing", f"{key}Lines"):
                spacing.attrib.pop(w(stale), None)
    if height is not None:  # fixed baseline distance (TeX baselineskip); atLeast grows for taller content
        spacing.set(w("line"), str(height.twips))
        spacing.set(w("lineRule"), "exact" if rule == "exact" else "atLeast")
    elif line is not None:
        spacing.set(w("line"), str(_half_up(240 * line)))
        spacing.set(w("lineRule"), "auto")


def _set_indent(ppr_element: etree._Element, style: TextStyle) -> None:
    """Merge into ``w:ind``; hanging and first-line exclude each other, and the ``*Chars`` variants would override twips."""
    indent = ensure_child(ppr_element, "ind", PPR_ORDER)

    def put(attribute: str, value: Length, *stale: str) -> None:
        indent.set(w(attribute), str(value.twips))
        for name in stale:
            indent.attrib.pop(w(name), None)

    if style.indent_left is not None:
        put("left", style.indent_left, "start", "leftChars", "startChars")
    if style.indent_hanging is not None:
        put("hanging", style.indent_hanging, "firstLine", "firstLineChars", "hangingChars")
    elif style.indent_first_line is not None:
        put("firstLine", style.indent_first_line, "hanging", "hangingChars", "firstLineChars")


def _apply_paragraph_properties(ppr_element: etree._Element, style: TextStyle) -> None:
    if style.keep_next is not None:
        _toggle(ppr_element, ("keepNext",), PPR_ORDER, style.keep_next)
    if style.keep_lines is not None:
        _toggle(ppr_element, ("keepLines",), PPR_ORDER, style.keep_lines)
    if style.widow_control is not None:
        _toggle(ppr_element, ("widowControl",), PPR_ORDER, style.widow_control)
    if style.page_break_before is not None:
        _toggle(ppr_element, ("pageBreakBefore",), PPR_ORDER, style.page_break_before)
    if style.before_pt is not None or style.after_pt is not None or style.line_spacing is not None or style.line_height is not None:
        _set_spacing(ppr_element, before=style.before_pt, after=style.after_pt, line=style.line_spacing,
                     height=style.line_height, rule=style.line_rule)
    if style.indent_left is not None or style.indent_first_line is not None or style.indent_hanging is not None:
        _set_indent(ppr_element, style)
    if style.alignment is not None:
        set_child(ppr_element, "jc", PPR_ORDER, val=_JC[style.alignment])


def _is_set(style: TextStyle, names: Iterable[str]) -> bool:
    return any(getattr(style, name) is not None for name in names)


def _apply_text_style(style_element: etree._Element, text: TextStyle) -> None:
    """Write ``text`` into a style; unset fields leave the style as it is."""
    style_id = style_element.get(w("styleId"), "")
    if text.indent_first_line is not None and text.indent_hanging is not None:
        raise BuildError(f"L2W-E-REFDOC-005: style {style_id!r} sets both indent_first_line and indent_hanging")
    if style_element.get(w("type")) == "character":
        if _is_set(text, _PARAGRAPH_FIELDS):
            raise BuildError(f"L2W-E-REFDOC-006: {style_id!r} is a character style and cannot carry paragraph properties")
    elif _is_set(text, _PARAGRAPH_FIELDS):
        _apply_paragraph_properties(ensure_child(style_element, "pPr", STYLE_ORDER), text)
    if _is_set(text, _RUN_FIELDS):
        _apply_run_properties(ensure_child(style_element, "rPr", STYLE_ORDER), text)


def _linked_character_style(styles: etree._Element, style_element: etree._Element) -> etree._Element | None:
    """The character style that mirrors a paragraph style (both ``w:link`` each other), if any."""
    link = style_element.find(w("link"))
    if link is None:
        return None
    target = _find(styles, link.get(w("val"), ""))
    if target is None or target.get(w("type")) != "character":
        return None
    back = target.find(w("link"))
    return target if back is not None and back.get(w("val")) == style_element.get(w("styleId")) else None


# --------------------------------------------------------------------------- what each style gets


def _style_spec(style_id: str, layout: ResolvedLayout) -> TextStyle | None:
    """The :class:`TextStyle` the layout prescribes for ``style_id`` (``None``: the layout says nothing about it).

    Running paragraphs take ``layout.paragraph`` unless the ``body`` role sets that very property; ``Compact`` never
    inherits Body Text's first-line indent into table cells; figure paragraphs carry the figure alignment and sit
    flush against their caption.
    """
    if style_id == ids.CAPTIONED_FIGURE:
        return TextStyle(alignment=layout.figures.alignment, keep_next=True, before_pt=0, after_pt=0)
    role = _STYLE_ROLE.get(style_id)
    text = layout.typography.get(role) if role is not None else None
    if style_id == ids.CENTERED:
        # \begin{center}: centred lines without the body's first-line indent, unless the profile says otherwise
        defaults = TextStyle(alignment="center", indent_first_line=Length(0))
        overrides = {f.name: getattr(text, f.name) for f in fields(TextStyle) if text is not None and getattr(text, f.name) is not None}
        return replace(defaults, **overrides)
    if role == "body":
        defaults = TextStyle(
            line_spacing=layout.paragraph.line_spacing, before_pt=layout.paragraph.before_pt, after_pt=layout.paragraph.after_pt,
            line_height=layout.paragraph.line_height, line_rule=layout.paragraph.line_rule
        )
        overrides = {f.name: getattr(text, f.name) for f in fields(TextStyle) if text is not None and getattr(text, f.name) is not None}
        return replace(defaults, **overrides)
    if style_id == ids.COMPACT and text is not None and text.indent_first_line is None and text.indent_hanging is None:
        return replace(text, indent_first_line=Length(0))
    return text


def _parent_of(styles: etree._Element, style_id: str) -> str | None:
    """The style a new ``style_id`` is based on: its natural parent when defined, else Normal (``None`` for Normal itself)."""
    if style_id == ids.NORMAL:
        return None
    if style_id in ids.CHARACTER_STYLES:
        return DEFAULT_PARAGRAPH_FONT
    parent = _PARENTS.get(style_id, ids.NORMAL)
    if _find(styles, parent) is not None:
        return parent
    return ids.NORMAL if _find(styles, ids.NORMAL) is not None else None


def _create_style(styles: etree._Element, style_id: str) -> etree._Element:
    """A new, empty style appended to the part: basedOn its natural parent, listed in the style gallery."""
    character = style_id in ids.CHARACTER_STYLES
    style = etree.SubElement(styles, w("style"))
    style.set(w("type"), "character" if character else "paragraph")
    if style_id == ids.NORMAL:
        style.set(w("default"), "1")
    if style_id not in _WORD_BUILTIN:
        style.set(w("customStyle"), "1")
    style.set(w("styleId"), style_id)
    set_child(style, "name", STYLE_ORDER, val=ids.STYLE_NAMES[style_id])
    parent = _parent_of(styles, style_id)
    if parent is not None:
        set_child(style, "basedOn", STYLE_ORDER, val=parent)
    if style_id in _NEXT:
        set_child(style, "next", STYLE_ORDER, val=_NEXT[style_id])
    if style_id in ids.HEADING:
        set_child(style, "uiPriority", STYLE_ORDER, val="9")
    if style_id in ids.TOC_LEVELS:  # as Word declares its built-in TOC styles
        set_child(style, "uiPriority", STYLE_ORDER, val="39")
        set_child(style, "unhideWhenUsed", STYLE_ORDER)
    set_child(style, "qFormat", STYLE_ORDER)
    if style_id in ids.HEADING:
        outline = ensure_child(ensure_child(style, "pPr", STYLE_ORDER), "outlineLvl", PPR_ORDER)
        outline.set(w("val"), str(ids.HEADING.index(style_id)))
    return style


def _initialise_style(styles: etree._Element, style_id: str, layout: ResolvedLayout) -> etree._Element:
    style = _create_style(styles, style_id)
    spec = _style_spec(style_id, layout)
    if spec is not None:
        _apply_text_style(style, spec)
    return style


def _managed_style_ids(layout: ResolvedLayout) -> list[str]:
    """Style ids the layout prescribes something for, in a stable order (roles first, then the figure style)."""
    managed = [
        style_id
        for role, style_ids in ids.ROLE_STYLES.items()
        if role in layout.typography or role == "body"
        for style_id in style_ids
    ]
    return [*managed, ids.CAPTIONED_FIGURE, *([ids.CENTERED] if ids.CENTERED not in managed else [])]


# --------------------------------------------------------------------------- document defaults


def _apply_document_defaults(styles: etree._Element, layout: ResolvedLayout) -> None:
    """So every unstyled run and paragraph follows the profile, not Pandoc's theme font and 10 pt paragraph gap."""
    defaults = styles.find(w("docDefaults"))
    if defaults is None:
        defaults = etree.Element(w("docDefaults"))
        styles.insert(0, defaults)
    default_text = layout.typography.get("default")
    if default_text is not None and _is_set(default_text, ("font", "size_pt")):
        run_defaults = ensure_child(defaults, "rPrDefault", ("rPrDefault", "pPrDefault"))
        _apply_run_properties(ensure_child(run_defaults, "rPr", ("rPr",)), TextStyle(font=default_text.font, size_pt=default_text.size_pt))
    paragraph_defaults = ensure_child(defaults, "pPrDefault", ("rPrDefault", "pPrDefault"))
    paragraph = layout.paragraph
    _set_spacing(
        ensure_child(paragraph_defaults, "pPr", ("pPr",)),
        before=paragraph.before_pt, after=paragraph.after_pt, line=paragraph.line_spacing,
        height=paragraph.line_height, rule=paragraph.line_rule,
    )


def _remove_theme_colour(rpr: etree._Element) -> None:
    color = rpr.find(w("color"))
    if color is not None and color.get(w("themeColor")) is not None:
        rpr.remove(color)


def _strip_pandoc_theme(styles: etree._Element) -> None:
    """Headings and title: no theme fonts (inherit the profile's text font) and no theme colour (inherit the text colour)."""
    for style_id in _THEMED:
        style = _find(styles, style_id)
        if style is None:
            continue
        for target in (style, _linked_character_style(styles, style)):
            rpr = target.find(w("rPr")) if target is not None else None
            if rpr is not None:
                _strip_theme_fonts(rpr)
                _remove_theme_colour(rpr)


# --------------------------------------------------------------------------- table styles


def _table_style(
    style_id: str,
    *,
    borders: Mapping[str, int],
    margins: Mapping[str, int],
    header_bold: bool = False,
    header_rule: bool = False,
    header_row: bool = False,
) -> etree._Element:
    """A table style. ``borders`` maps a side to its rule width (eighths of a point); absent sides are ``nil``."""
    style = etree.Element(w("style"))
    style.set(w("type"), "table")
    style.set(w("styleId"), style_id)
    set_child(style, "name", STYLE_ORDER, val=ids.STYLE_NAMES[style_id])
    set_child(style, "qFormat", STYLE_ORDER)
    table_properties = ensure_child(style, "tblPr", STYLE_ORDER)
    set_child(table_properties, "tblInd", TBLPR_ORDER, w="0", type="dxa")
    border_element = ensure_child(table_properties, "tblBorders", TBLPR_ORDER)
    for side in _BORDER_SIDES:
        _border(border_element, side, borders.get(side))
    cell_margins = ensure_child(table_properties, "tblCellMar", TBLPR_ORDER)
    for side in ("top", "left", "bottom", "right"):
        etree.SubElement(cell_margins, w(side)).attrib.update({w("w"): str(margins[side]), w("type"): "dxa"})
    if header_row:
        _header_row_style(style, bold=header_bold, rule=header_rule)
    return style


def _border(parent: etree._Element, side: str, eighths: int | None) -> None:
    border = etree.SubElement(parent, w(side))
    if eighths is None:
        border.set(w("val"), "nil")
        return
    border.attrib.update({w("val"): "single", w("sz"): str(eighths), w("space"): "0", w("color"): "auto"})


def _header_row_style(style: etree._Element, *, bold: bool, rule: bool) -> None:
    """``tblStylePr firstRow`` (Word applies it because Pandoc writes ``tblLook firstRow="1"``)."""
    conditional = etree.SubElement(style, w("tblStylePr"))
    conditional.set(w("type"), "firstRow")
    if bold:
        rpr = etree.SubElement(conditional, w("rPr"))
        _toggle(rpr, ("b", "bCs"), RPR_ORDER, True)
    cell = etree.SubElement(conditional, w("tcPr"))
    if rule:
        _border(etree.SubElement(cell, w("tcBorders")), "bottom", _HAIRLINE_EIGHTHS)
    etree.SubElement(cell, w("vAlign")).set(w("val"), "bottom")


def _data_table_style(layout: ResolvedLayout) -> etree._Element:
    tables = layout.tables
    rules = {"none": {}, "rules": {"top": _RULE_EIGHTHS, "bottom": _RULE_EIGHTHS}}
    grid = dict.fromkeys(_BORDER_SIDES, _HAIRLINE_EIGHTHS)
    margins = {
        "top": tables.margin_top.twips, "left": tables.margin_left.twips,
        "bottom": tables.margin_bottom.twips, "right": tables.margin_right.twips,
    }
    return _table_style(
        ids.TABLE,
        borders=grid if tables.borders == "grid" else rules[tables.borders],
        margins=margins,
        header_bold=tables.header_emphasis == "bold",
        header_rule=tables.borders == "rules",
        header_row=True,
    )


def _layout_table_style(style_id: str) -> etree._Element:
    side = _AUTHOR_BLOCK_SIDE_MARGIN_TWIPS if style_id == ids.TABLE_AUTHOR_BLOCK else 0
    return _table_style(style_id, borders={}, margins={"top": 0, "left": side, "bottom": 0, "right": side})


def _table_style_for(style_id: str, layout: ResolvedLayout) -> etree._Element:
    return _data_table_style(layout) if style_id == ids.TABLE else _layout_table_style(style_id)


def _define_table_styles(styles: etree._Element, layout: ResolvedLayout) -> list[str]:
    """Define (or overwrite) the three table styles; returns the ids that did not exist before."""
    created: list[str] = []
    for style_id in _TABLE_STYLES:
        replacement = _table_style_for(style_id, layout)
        existing = _find(styles, style_id)
        if existing is None:
            styles.append(replacement)
            created.append(style_id)
            continue
        if existing.get(w("default")) is not None:
            replacement.set(w("default"), existing.get(w("default"), ""))
        styles.replace(existing, replacement)
    return created


# --------------------------------------------------------------------------- public API


def apply_size_styles(styles: etree._Element, document: etree._Element, sizes: dict[str, float]) -> list[str]:
    """Character styles ``Size<command>`` (from {\\Large ...} groups) get the profile's point size.

    Returns the style ids set. A command without a size in ``sizes`` is left as Pandoc wrote it.
    """
    used = sorted({r.get(w("val")) for r in document.iter(w("rStyle")) if (r.get(w("val")) or "").startswith("Size")})
    done: list[str] = []
    for style_id in used:
        command = style_id[len("Size"):]
        if command not in sizes:
            continue
        style = _find(styles, style_id)
        if style is None:
            style = etree.SubElement(styles, w("style"))
            style.set(w("type"), "character")
            style.set(w("customStyle"), "1")
            style.set(w("styleId"), style_id)
            set_child(style, "name", STYLE_ORDER, val=f"Size {command}")
            set_child(style, "basedOn", STYLE_ORDER, val=DEFAULT_PARAGRAPH_FONT)
        half_points = str(_half_up(sizes[command] * 2))
        rpr = ensure_child(style, "rPr", STYLE_ORDER)
        set_child(rpr, "sz", RPR_ORDER, val=half_points)
        set_child(rpr, "szCs", RPR_ORDER, val=half_points)
        done.append(style_id)
    return done


def _set_right_tab(style: etree._Element, layout: ResolvedLayout, leader: str) -> None:
    """A right tab stop at the text width (where contents entries put their page number)."""
    ppr_element = ensure_child(style, "pPr", STYLE_ORDER)
    tabs = ensure_child(ppr_element, "tabs", PPR_ORDER)
    for old in tabs.findall(w("tab")):
        tabs.remove(old)
    tab = etree.SubElement(tabs, w("tab"))
    tab.set(w("val"), "right")
    tab.set(w("leader"), leader)
    tab.set(w("pos"), str(layout.page.text_width.twips))


def apply_typography(styles: etree._Element, layout: ResolvedLayout) -> list[str]:
    """Patch a Pandoc-style styles part in place from the profile; returns the style ids that had to be created."""
    created: list[str] = []
    _apply_document_defaults(styles, layout)
    for style_id in _managed_style_ids(layout):
        spec = _style_spec(style_id, layout)
        if spec is None:
            continue
        style = _find(styles, style_id)
        if style is None:
            style = _create_style(styles, style_id)
            created.append(style_id)
        _apply_text_style(style, spec)
        if spec.right_tab_leader is not None:
            _set_right_tab(style, layout, spec.right_tab_leader)
        mirror = _linked_character_style(styles, style)
        if mirror is not None and _is_set(spec, _RUN_FIELDS):
            _apply_text_style(mirror, TextStyle(**{name: getattr(spec, name) for name in _RUN_FIELDS}))
    _strip_pandoc_theme(styles)
    created.extend(_define_table_styles(styles, layout))
    return created


def apply_style_spec(styles: etree._Element, style_id: str, spec: TextStyle) -> etree._Element:
    """Write ``spec`` into ``style_id`` (created, based on its natural parent, when missing)."""
    style = _find(styles, style_id)
    if style is None:
        style = _create_style(styles, style_id)
    elif style.find(w("basedOn")) is None and _parent_of(styles, style_id) is not None:
        set_child(style, "basedOn", STYLE_ORDER, val=_parent_of(styles, style_id))
    _apply_text_style(style, spec)
    return style


def apply_direct_spec(paragraph: etree._Element, spec: TextStyle) -> None:
    """``spec`` as direct formatting of one paragraph: paragraph properties, and the run size on every run."""
    if _is_set(spec, _PARAGRAPH_FIELDS):
        _apply_paragraph_properties(ppr(paragraph), spec)
    if spec.size_pt is not None:
        for run in paragraph.iter(w("r")):
            rpr = run.find(w("rPr"))
            if rpr is None:  # w:rPr is always the first child of a run
                rpr = etree.Element(w("rPr"))
                run.insert(0, rpr)
            _apply_run_properties(rpr, TextStyle(size_pt=spec.size_pt))


def style_spacing_after(styles: etree._Element, style_id: str) -> int:
    """Effective space after (twips) of a paragraph style: its own, its ancestors', then the document default."""
    seen: set[str] = set()
    current = _find(styles, style_id)
    while current is not None and style_id not in seen:
        seen.add(style_id)
        spacing = current.find(f"{w('pPr')}/{w('spacing')}")
        if spacing is not None and spacing.get(w("after")) is not None:
            return int(spacing.get(w("after")))
        parent = current.find(w("basedOn"))
        style_id = parent.get(w("val"), "") if parent is not None else ""
        current = _find(styles, style_id) if style_id else None
    default = styles.find(f"{w('docDefaults')}/{w('pPrDefault')}/{w('pPr')}/{w('spacing')}")
    return int(default.get(w("after"), "0")) if default is not None else 0


def ensure_required_styles(styles: etree._Element, layout: ResolvedLayout) -> list[str]:
    """For external templates: add the styles the layout code needs; existing styles are never modified."""
    wanted = dict.fromkeys([*ids.REQUIRED_STYLES, *_managed_style_ids(layout), *_TABLE_STYLES])
    present = style_ids_in(styles)
    created: list[str] = []
    for style_id in wanted:
        if style_id in present:
            continue
        if style_id in _TABLE_STYLES:
            styles.append(_table_style_for(style_id, layout))
        else:
            _initialise_style(styles, style_id, layout)
        created.append(style_id)
    return created


# --------------------------------------------------------------------------- description


def _style_chain(index: Mapping[str, etree._Element], style_id: str) -> list[etree._Element]:
    """The style and its ``basedOn`` ancestors, nearest first (cycle-safe)."""
    chain: list[etree._Element] = []
    seen: set[str] = set()
    current: str | None = style_id
    while current is not None and current in index and current not in seen:
        seen.add(current)
        chain.append(index[current])
        based_on = index[current].find(w("basedOn"))
        current = based_on.get(w("val")) if based_on is not None else None
    return chain


def _is_on(element: etree._Element) -> bool:
    return element.get(w("val"), "1").lower() not in ("0", "false", "off")


def _inherited(chain: list[etree._Element], container: str, name: str) -> etree._Element | None:
    """``w:<container>/w:<name>`` of the nearest style in ``chain`` that defines it."""
    for style in chain:
        found = style.find(f"{w(container)}/{w(name)}")
        if found is not None:
            return found
    return None


def _attribute(
    chain: list[etree._Element], defaults: etree._Element | None, container: str, name: str, attribute: str
) -> str | None:
    """Attribute of an inherited property, falling back to ``w:docDefaults``."""
    wrapper = "rPrDefault" if container == "rPr" else "pPrDefault"
    nodes = [style.find(f"{w(container)}/{w(name)}") for style in chain]
    if defaults is not None:
        nodes.append(defaults.find(f"{w(wrapper)}/{w(container)}/{w(name)}"))
    for found in nodes:
        if found is not None and found.get(w(attribute)) is not None:
            return found.get(w(attribute))
    return None


def _flag(chain: list[etree._Element], container: str, name: str) -> bool:
    found = _inherited(chain, container, name)
    return _is_on(found) if found is not None else False


def _caps_value(chain: list[etree._Element]) -> str:
    """``all`` / ``small`` / ``none``: the nearest style that mentions either caps property decides."""
    for style in chain:
        caps, small = style.find(f"{w('rPr')}/{w('caps')}"), style.find(f"{w('rPr')}/{w('smallCaps')}")
        if caps is not None or small is not None:
            if caps is not None and _is_on(caps):
                return "all"
            return "small" if small is not None and _is_on(small) else "none"
    return "none"


def _scaled(value: str | None, divisor: int) -> float | None:
    return int(value) / divisor if value is not None else None


def describe_styles(styles: etree._Element, style_id_list: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Effective properties of the given styles (``basedOn`` chain and ``docDefaults`` resolved).

    Keys per style: name, basedOn, font (``w:ascii``), size_pt, bold, italic, caps (none/all/small), alignment
    (left/right/center/justify), before_pt, after_pt, keep_next. Ids the part does not define are omitted.
    """
    index = {sid: style for style in styles.iterfind(w("style")) if (sid := style.get(w("styleId"))) is not None}
    defaults = styles.find(w("docDefaults"))
    out: dict[str, dict[str, Any]] = {}
    for style_id in style_id_list:
        if style_id not in index:
            continue
        chain = _style_chain(index, style_id)
        name, based_on = index[style_id].find(w("name")), index[style_id].find(w("basedOn"))
        alignment = _attribute(chain, defaults, "pPr", "jc", "val")
        out[style_id] = {
            "name": name.get(w("val")) if name is not None else None,
            "basedOn": based_on.get(w("val")) if based_on is not None else None,
            "font": _attribute(chain, defaults, "rPr", "rFonts", "ascii"),
            "size_pt": _scaled(_attribute(chain, defaults, "rPr", "sz", "val"), 2),
            "bold": _flag(chain, "rPr", "b"),
            "italic": _flag(chain, "rPr", "i"),
            "caps": _caps_value(chain),
            "alignment": _JC_BACK.get(alignment, "left") if alignment is not None else "left",
            "before_pt": _scaled(_attribute(chain, defaults, "pPr", "spacing", "before"), 20) or 0.0,
            "after_pt": _scaled(_attribute(chain, defaults, "pPr", "spacing", "after"), 20) or 0.0,
            "keep_next": _flag(chain, "pPr", "keepNext"),
        }
    return out


__all__ = ["apply_typography", "describe_styles", "ensure_required_styles", "style_ids_in"]
