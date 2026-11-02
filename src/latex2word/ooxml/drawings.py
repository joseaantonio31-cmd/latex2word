"""Figure extents and caption placement for a Pandoc DOCX (publisher-neutral).

Pandoc sizes every figure from the ``\\includegraphics`` width it was given, but
it knows nothing about the *column* the figure lands in. This module resolves the
requested width of each figure against the geometry of the section the figure is
in (``\\columnwidth``, ``\\linewidth`` and ``\\textwidth`` are distinct bases),
clamps it to the column unless the profile allows overflow, and rewrites both
extents that Word reads (``wp:extent`` and the ``a:ext`` of ``pic:spPr``).

Invariants:

* media bytes, ``a:blip``/``asvg:svgBlip`` relationships, ``wp:docPr`` and all
  bookmarks are never touched; figures are never rasterised;
* the aspect ratio is taken from the drawing's *existing* extent, so a rounding
  difference in the requested width can never distort the picture;
* a figure always fits its column here (``span == "column"``); double-column
  floats are a deliberate future extension, not an implicit behaviour.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lxml import etree

from latex2word.diagnostics import Severity
from latex2word.layout import ResolvedLayout, resolve_figure_width
from latex2word.ooxml import style_ids
from latex2word.ooxml.namespaces import qn, w, xpath
from latex2word.ooxml.package import OpcPackage
from latex2word.ooxml.sections import SectionPlan
from latex2word.ooxml.xmlutil import paragraph_style
from latex2word.source_graph import Finding
from latex2word.units import EMU_PER_INCH, Length

ASPECT_TOLERANCE = 0.02
_RELATIONSHIP_ID = qn("r:embed")
_BOOKMARKS = (w("bookmarkStart"), w("bookmarkEnd"))


@dataclass(frozen=True)
class FigureSpec:
    """What the build knows about one figure (``width`` is a ``WidthIR`` dict)."""

    id: str
    sha256: str | None
    width: Mapping[str, Any] | None
    aspect: float | None  # width / height


@dataclass(frozen=True)
class FigureOutcome:
    """The extent finally written for one drawing."""

    id: str | None
    matched: bool
    basis: str | None
    requested_emu: int | None
    available_emu: int
    cx_emu: int
    cy_emu: int
    clamped: bool
    section: str  # frontmatter | body
    span: str = "column"

    @property
    def cx_in(self) -> float:
        return self.cx_emu / EMU_PER_INCH

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "matched": self.matched, "basis": self.basis, "requested_emu": self.requested_emu,
            "available_emu": self.available_emu, "cx_emu": self.cx_emu, "cy_emu": self.cy_emu,
            "cx_in": round(self.cx_in, 6), "clamped": self.clamped, "section": self.section, "span": self.span,
        }


def _finding(severity: Severity, number: int, message: str) -> Finding:
    return Finding(f"L2W-{severity.value[0].upper()}-LAYOUT-{number:03d}", severity, message)


def _drawings(document: etree._Element) -> list[etree._Element]:
    """Every ``wp:inline``/``wp:anchor`` that embeds a picture, in document order."""
    return [
        node for node in document.iter(qn("wp:inline"), qn("wp:anchor"))
        if node.find(f".//{qn('a:blip')}") is not None
    ]


def _media_hashes(package: OpcPackage, drawing: etree._Element, targets: Mapping[str, str]) -> set[str]:
    """sha256 of every media part the drawing's raster and SVG blips point at."""
    hashes: set[str] = set()
    for blip in drawing.iter(qn("a:blip"), qn("asvg:svgBlip")):
        part = targets.get(blip.get(_RELATIONSHIP_ID, ""))
        if part is not None:
            hashes.add(hashlib.sha256(package.read(part)).hexdigest())
    return hashes


def _paragraph_of(drawing: etree._Element) -> etree._Element:
    for ancestor in drawing.iterancestors():
        if ancestor.tag == w("p"):
            return ancestor
    return drawing


class _SpecMatcher:
    """Pairs drawings with specs: by media hash first, then hash-less specs in document order."""

    def __init__(self, specs: Sequence[FigureSpec]) -> None:
        self._by_hash: dict[str, list[FigureSpec]] = {}
        for spec in specs:
            if spec.sha256 is not None:
                self._by_hash.setdefault(spec.sha256, []).append(spec)
        self._anonymous = [spec for spec in specs if spec.sha256 is None]
        self._used: set[int] = set()

    def by_hash(self, hashes: set[str]) -> FigureSpec | None:
        for digest in sorted(hashes):
            candidates = self._by_hash.get(digest)
            if not candidates:
                continue
            fresh = [spec for spec in candidates if id(spec) not in self._used]
            chosen = (fresh or candidates)[0]
            self._used.add(id(chosen))
            return chosen
        return None

    def next_anonymous(self) -> FigureSpec | None:
        for spec in self._anonymous:
            if id(spec) not in self._used:
                self._used.add(id(spec))
                return spec
        return None


def _extents(drawing: etree._Element) -> list[etree._Element]:
    """``wp:extent`` and the picture's ``a:ext`` (the two places Word reads the size from)."""
    nodes = xpath(drawing, "./wp:extent | .//pic:spPr/a:xfrm/a:ext")
    return [node for node in nodes if node.get("cx") is not None]


def _scaled_height(cx: int, old_cx: int, old_cy: int) -> int:
    """``round(cx * old_cy / old_cx)`` in exact integer arithmetic."""
    return (2 * cx * old_cy + old_cx) // (2 * old_cx)


def _write_extents(drawing: etree._Element, cx: int, cy: int) -> None:
    for node in _extents(drawing):
        node.set("cx", str(cx))
        node.set("cy", str(cy))


def _inches(emu: int) -> str:
    return f"{Length(emu).in_:.4f}in"


def _fit_drawing(
    drawing: etree._Element, spec: FigureSpec | None, plan: SectionPlan, label: str
) -> tuple[FigureOutcome | None, list[Finding]]:
    layout: ResolvedLayout = plan.layout
    paragraph = _paragraph_of(drawing)
    geometry = plan.geometry_for(paragraph)
    section = "frontmatter" if plan.is_frontmatter(paragraph) else "body"
    available = geometry.column_width.emu
    extent = drawing.find(qn("wp:extent"))
    old_cx = int(extent.get("cx", "0")) if extent is not None else 0
    old_cy = int(extent.get("cy", "0")) if extent is not None else 0
    findings: list[Finding] = []
    if old_cx <= 0 or old_cy <= 0:
        findings.append(_finding(Severity.ERROR, 13, f"figure {label}: drawing has no positive extent; left unchanged"))
        return None, findings
    if spec is None:  # nothing is known about this drawing: report it, never resize it
        return FigureOutcome(None, False, None, None, available, old_cx, old_cy, False, section), findings
    resolved = resolve_figure_width(spec.width, geometry, layout)
    requested = resolved.requested.emu if resolved.requested is not None else None
    cx = requested if requested is not None else old_cx
    clamped = False
    if cx > available and not layout.figures.allow_overflow:
        findings.append(
            _finding(
                Severity.WARNING, 11,
                f"figure {label}: requested width {_inches(cx)} exceeds the available column width "
                f"{_inches(available)}; clamped to the column",
            )
        )
        cx, clamped = available, True
    cy = _scaled_height(cx, old_cx, old_cy)
    if spec.aspect:
        old_aspect = old_cx / old_cy
        if abs(old_aspect - spec.aspect) > ASPECT_TOLERANCE * spec.aspect:
            findings.append(
                _finding(
                    Severity.WARNING, 12,
                    f"figure {label}: drawing aspect {old_aspect:.4f} differs from the asset aspect "
                    f"{spec.aspect:.4f} by more than {ASPECT_TOLERANCE:.0%}",
                )
            )
    _write_extents(drawing, cx, cy)
    outcome = FigureOutcome(
        spec.id, True, resolved.basis, requested, available, cx, cy, clamped, section,
    )
    return outcome, findings


def apply_figure_extents(
    package: OpcPackage, document: etree._Element, plan: SectionPlan, specs: Sequence[FigureSpec]
) -> tuple[list[FigureOutcome], list[Finding]]:
    """Resize every picture drawing to its resolved width, clamped to the column (see module docstring)."""
    targets = package.relationship_targets("word/document.xml")
    matcher = _SpecMatcher(specs)
    drawings = _drawings(document)
    matched = [matcher.by_hash(_media_hashes(package, drawing, targets)) for drawing in drawings]
    outcomes: list[FigureOutcome] = []
    findings: list[Finding] = []
    for index, (drawing, spec) in enumerate(zip(drawings, matched, strict=True), start=1):
        spec = spec or matcher.next_anonymous()
        label = repr(spec.id) if spec else f"#{index}"
        if spec is None:
            findings.append(
                _finding(Severity.WARNING, 10, f"figure {label}: no figure record matches this drawing; intrinsic size kept")
            )
        outcome, extra = _fit_drawing(drawing, spec, plan, label)
        findings.extend(extra)
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes, findings


# --------------------------------------------------------------------------- caption placement


def _is_data_table(element: etree._Element) -> bool:
    if element.tag != w("tbl"):
        return False
    style = element.find(f"{w('tblPr')}/{w('tblStyle')}")
    return style is None or style.get(w("val")) not in style_ids.LAYOUT_TABLE_STYLES


def _kind(element: etree._Element, item_style: str | None, caption_style: str) -> str | None:
    if element.tag == w("p") and paragraph_style(element) == caption_style:
        return "caption"
    if item_style is None:
        return "item" if _is_data_table(element) else "other"
    if element.tag == w("p") and paragraph_style(element) == item_style:
        return "item"
    return "other"


def _pairs(
    parent: etree._Element, item_style: str | None, caption_style: str
) -> list[tuple[etree._Element, etree._Element, bool]]:
    """``(item, caption, caption_is_above)`` pairs of one parent.

    A single left-to-right scan: an element waits for a partner that follows it
    with nothing but bookmarks in between. Scanning in order is what keeps
    ``Fig Cap Fig Cap`` and ``Cap Fig Cap Fig`` unambiguous.
    """
    pairs: list[tuple[etree._Element, etree._Element, bool]] = []
    waiting: tuple[str, etree._Element] | None = None
    for child in parent:
        if child.tag in _BOOKMARKS:
            continue
        kind = _kind(child, item_style, caption_style)
        if kind == "other":
            waiting = None
        elif waiting is not None and waiting[0] != kind:
            item, caption = (child, waiting[1]) if kind == "item" else (waiting[1], child)
            pairs.append((item, caption, kind == "item"))
            waiting = None
        else:
            waiting = (kind, child)
    return pairs


def _place(item: etree._Element, caption: etree._Element, above: bool) -> None:
    """Move only the caption paragraph next to ``item``; sibling bookmarks stay where they are."""
    (item.addprevious if above else item.addnext)(caption)


def _arrange(document: etree._Element, item_style: str | None, caption_style: str, placement: str, noun: str) -> list[Finding]:
    candidates = xpath(document, "//w:p | //w:tbl")
    parents: list[etree._Element] = []
    for element in candidates:
        parent = element.getparent()
        if parent is not None and all(parent is not seen for seen in parents) and (
            _kind(element, item_style, caption_style) in ("item", "caption")
        ):
            parents.append(parent)
    findings: list[Finding] = []
    want_above = placement == "above"
    for parent in parents:
        for item, caption, above in _pairs(parent, item_style, caption_style):
            if above == want_above:
                continue
            _place(item, caption, want_above)
            findings.append(_finding(Severity.INFO, 14, f"{noun} caption moved {placement} its {noun}"))
    return findings


def apply_caption_placement(document: etree._Element, layout: ResolvedLayout) -> list[Finding]:
    """Move captions so figures and tables follow ``layout.figures`` / ``layout.tables`` ``caption_placement``.

    Pandoc writes figure captions below and table captions above; nothing
    moves when the profile asks for exactly that.
    """
    findings = _arrange(
        document, style_ids.CAPTIONED_FIGURE, style_ids.IMAGE_CAPTION, layout.figures.caption_placement, "figure"
    )
    findings += _arrange(document, None, style_ids.TABLE_CAPTION, layout.tables.caption_placement, "table")
    return findings


__all__ = ["ASPECT_TOLERANCE", "FigureOutcome", "FigureSpec", "apply_caption_placement", "apply_figure_extents"]
