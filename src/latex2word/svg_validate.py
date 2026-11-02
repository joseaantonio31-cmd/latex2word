"""Renderer-independent SVG validation (Python standard library only).

Validation never trusts the producer: it checks well-formedness, character
hygiene, a usable size, real geometry, and that nothing is linked from outside
the file. Metrics are recorded for the figure manifest and for backend
comparison.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from latex2word.diagnostics import Severity

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# XML 1.0 forbids these C0 controls; they appeared in a PDF->SVG route of an earlier pipeline.
_FORBIDDEN_CONTROLS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_URL_REF = re.compile(r"url\(\s*['\"]?([^)'\"\s]+)")
_CSS_IMPORT = re.compile(r"@import\s+(?:url\()?\s*['\"]?([^)'\";\s]+)")
_LENGTH = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(px|pt|pc|mm|cm|in|em|ex|%)?\s*$")
_TO_PX = {None: 1.0, "px": 1.0, "pt": 4 / 3, "pc": 16.0, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0}
_FONT_FAMILY_PROPERTY = re.compile(r"font-family\s*:\s*([^;}]+)")
_SHAPES = frozenset({"rect", "circle", "ellipse", "line", "polyline", "polygon"})


@dataclass(frozen=True)
class SvgIssue:
    code: str
    severity: Severity
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity.value, "message": self.message}

    def __str__(self) -> str:
        return f"{self.severity.value}: {self.message} [{self.code}]"


@dataclass(frozen=True)
class SvgMetrics:
    size_bytes: int
    width: str | None
    height: str | None
    width_px: float | None
    height_px: float | None
    view_box: str | None
    paths: int
    shapes: int
    text: int
    images: int
    uses: int
    fonts: tuple[str, ...]
    control_characters: int
    replacement_characters: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "viewBox": self.view_box,
            "paths": self.paths,
            "shapes": self.shapes,
            "text": self.text,
            "images": self.images,
            "uses": self.uses,
            "fonts": list(self.fonts),
            "control_characters": self.control_characters,
            "replacement_characters": self.replacement_characters,
        }

    @property
    def path_dominated(self) -> bool:
        """True when paths outnumber text elements (text rendered as outlines)."""
        return self.paths > self.text


@dataclass(frozen=True)
class SvgReport:
    issues: tuple[SvgIssue, ...] = ()
    metrics: SvgMetrics | None = None
    sha256: str | None = field(default=None)

    @property
    def errors(self) -> list[SvgIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[SvgIssue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "sha256": self.sha256,
            "issues": [i.to_dict() for i in self.issues],
            "metrics": self.metrics.to_dict() if self.metrics else None,
        }


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_length(value: str | None) -> tuple[bool, float | None]:
    """``(is_valid, pixels)``; relative units (em, ex, %) are valid but have no pixel value."""
    if value is None:
        return True, None
    match = _LENGTH.match(value)
    if match is None:
        return False, None
    number = float(match.group(1))
    if number <= 0:
        return False, None
    unit = match.group(2)
    if unit in ("em", "ex", "%"):
        return True, None
    return True, round(number * _TO_PX[unit], 4)


def _parse_view_box(value: str) -> tuple[float, float, float, float] | None:
    parts = [p for p in re.split(r"[\s,]+", value.strip()) if p]
    if len(parts) != 4:
        return None
    try:
        x, y, w, h = (float(p) for p in parts)
    except ValueError:
        return None
    if not (w > 0 and h > 0):
        return None
    return x, y, w, h


def _iter_style_texts(root: ET.Element) -> Iterator[str]:
    for element in root.iter():
        if _local(element.tag) == "style" and element.text:
            yield element.text
        style = element.get("style")
        if style:
            yield style


def _font_names(root: ET.Element) -> tuple[str, ...]:
    names: set[str] = set()

    def add(value: str) -> None:
        for part in value.split(","):
            cleaned = part.strip().strip("'\"").strip()
            if cleaned:
                names.add(cleaned)

    for element in root.iter():
        local = _local(element.tag)
        family = element.get("font-family")
        if family:
            add(family)
        if local == "font-face" and element.get("font-family"):
            add(element.get("font-family", ""))
    for css in _iter_style_texts(root):
        for match in _FONT_FAMILY_PROPERTY.finditer(css):
            add(match.group(1))
    return tuple(sorted(names))


def _is_external(reference: str) -> bool:
    ref = reference.strip()
    return not (ref.startswith("#") or ref.lower().startswith("data:"))


def validate_svg_bytes(data: bytes, *, expect_vector: bool = True) -> SvgReport:
    """Validate SVG ``data``; see the module docstring for what is checked."""
    issues: list[SvgIssue] = []
    digest = hashlib.sha256(data).hexdigest()

    def error(code: str, message: str) -> None:
        issues.append(SvgIssue(code, Severity.ERROR, message))

    def warn(code: str, message: str) -> None:
        issues.append(SvgIssue(code, Severity.WARNING, message))

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        error("invalid-encoding", "file is not valid UTF-8")
        text = data.decode("utf-8", errors="replace")

    controls = len(_FORBIDDEN_CONTROLS.findall(text))
    if controls:
        error("control-characters", f"{controls} forbidden XML control character(s) in the file")
    replacements = text.count("\ufffd")
    if replacements:
        warn("replacement-characters", f"{replacements} U+FFFD replacement character(s) in the file")

    if "<!ENTITY" in text:
        error("unsafe-entity", "DOCTYPE entity declarations are not accepted")
        return SvgReport(tuple(issues), None, digest)

    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        error("malformed-xml", f"not well-formed XML: {exc}")
        return SvgReport(tuple(issues), None, digest)

    if _local(root.tag) != "svg":
        error("missing-svg-root", f"root element is <{_local(root.tag)}>, expected <svg>")
        return SvgReport(tuple(issues), None, digest)
    if root.tag != f"{{{SVG_NAMESPACE}}}svg":
        warn("missing-namespace", f"<svg> is not in the {SVG_NAMESPACE} namespace")

    width_attr, height_attr, view_box_attr = root.get("width"), root.get("height"), root.get("viewBox")
    width_ok, width_px = parse_length(width_attr)
    height_ok, height_px = parse_length(height_attr)
    if not width_ok:
        error("invalid-dimensions", f"invalid width {width_attr!r}")
    if not height_ok:
        error("invalid-dimensions", f"invalid height {height_attr!r}")
    view_box = _parse_view_box(view_box_attr) if view_box_attr is not None else None
    if view_box_attr is not None and view_box is None:
        error("invalid-viewbox", f"invalid viewBox {view_box_attr!r}")
    if view_box_attr is None and (width_attr is None or height_attr is None):
        error("missing-dimensions", "no viewBox and no complete width/height")
    elif view_box_attr is None:
        warn("missing-viewbox", "no viewBox; the drawing will not scale cleanly")
    elif width_attr is None or height_attr is None:
        warn("missing-size", "viewBox without explicit width/height")
    if width_px is None and height_px is None and view_box is not None and width_ok and height_ok:
        width_px, height_px = round(view_box[2], 4), round(view_box[3], 4)  # px by SVG default

    ids = {e.get("id") for e in root.iter() if e.get("id")}
    paths = shapes = texts = images = uses = 0
    external: list[str] = []
    unresolved: list[str] = []
    raster_types: set[str] = set()

    def check_reference(reference: str, where: str) -> None:
        if _is_external(reference):
            external.append(f"{where}: {reference}")
        elif reference.startswith("#") and reference[1:] not in ids:
            unresolved.append(f"{where}: {reference}")

    for element in root.iter():
        local = _local(element.tag)
        if local == "path":
            if (element.get("d") or "").strip():
                paths += 1
        elif local in _SHAPES:
            shapes += 1
        elif local == "text":
            texts += 1
        elif local == "image":
            images += 1
        elif local == "use":
            uses += 1
        for name, value in element.attrib.items():
            if name in ("href", _XLINK_HREF):
                check_reference(value, f"<{local}> href")
                if local == "image" and value.lower().startswith("data:"):
                    raster_types.add(value[5:].split(";", 1)[0].split(",", 1)[0] or "unknown")
            elif name == "style" or "url(" in value:
                for match in _URL_REF.finditer(value):
                    check_reference(match.group(1), f"<{local}> {name}")
    for css in _iter_style_texts(root):
        for match in _URL_REF.finditer(css):
            check_reference(match.group(1), "<style>")
        for match in _CSS_IMPORT.finditer(css):
            external.append(f"<style> @import: {match.group(1)}")

    for item in external:
        error("external-reference", f"external resource reference ({item})")
    for item in unresolved:
        error("unresolved-reference", f"reference to a missing id ({item})")
    if paths + shapes + texts + images == 0:
        error("empty-geometry", "no drawable geometry (paths, shapes, text or images)")
    if images and expect_vector:
        kinds = f" ({', '.join(sorted(raster_types))})" if raster_types else ""
        warn("embedded-raster", f"{images} <image> element(s){kinds} in output that should be vector")

    metrics = SvgMetrics(
        size_bytes=len(data),
        width=width_attr,
        height=height_attr,
        width_px=width_px,
        height_px=height_px,
        view_box=view_box_attr,
        paths=paths,
        shapes=shapes,
        text=texts,
        images=images,
        uses=uses,
        fonts=_font_names(root),
        control_characters=controls,
        replacement_characters=replacements,
    )
    return SvgReport(tuple(issues), metrics, digest)


def validate_svg_file(path: Path, *, expect_vector: bool = True) -> SvgReport:
    """Validate the SVG at ``path``; an unreadable file yields an error report."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return SvgReport((SvgIssue("unreadable", Severity.ERROR, f"cannot read SVG: {exc}"),))
    return validate_svg_bytes(data, expect_vector=expect_vector)
