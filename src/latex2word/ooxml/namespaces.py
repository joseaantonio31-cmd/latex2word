"""OOXML namespaces and qualified-name helpers (lxml ``{uri}local`` notation)."""

from __future__ import annotations

from lxml import etree

NS: dict[str, str] = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "asvg": "http://schemas.microsoft.com/office/drawing/2016/SVG/main",
    "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xml": "http://www.w3.org/XML/1998/namespace",
}
W_NS = NS["w"]


def qn(name: str) -> str:
    """``"w:p"`` -> ``"{http://...main}p"``."""
    prefix, _, local = name.partition(":")
    return f"{{{NS[prefix]}}}{local}"


def w(local: str) -> str:
    return f"{{{W_NS}}}{local}"


def local_name(element: etree._Element) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def xpath(element: etree._Element, expression: str) -> list[etree._Element]:
    """XPath with the standard OOXML prefixes bound."""
    return element.xpath(expression, namespaces=NS)  # type: ignore[no-any-return]


def text_of(element: etree._Element) -> str:
    """Visible text (w:t, w:tab as a tab, w:br as a newline); math text excluded."""
    parts: list[str] = []
    for node in element.iter(w("t"), w("tab"), w("br")):
        if any(ancestor.tag == qn("m:oMath") for ancestor in node.iterancestors()):
            continue
        parts.append(node.text or "" if node.tag == w("t") else "\t" if node.tag == w("tab") else "\n")
    return "".join(parts)
