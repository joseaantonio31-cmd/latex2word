"""Word fields written by the filters (tables of contents) and the setting that makes Word fill them.

The semantic filter emits ``TOC`` complex fields where the source had
``\\tableofcontents``, ``\\listoffigures`` and ``\\listoftables``. Their result is
a placeholder until Word updates them; ``w:updateFields`` in ``settings.xml``
asks Word to update all fields when the document is opened.
"""

from __future__ import annotations

from lxml import etree

from latex2word.ooxml.namespaces import w
from latex2word.ooxml.package import OpcPackage

# CT_Settings children that follow w:updateFields in the schema sequence (w:updateFields must precede them).
_AFTER_UPDATE_FIELDS = (
    "hdrShapeDefaults", "footnotePr", "endnotePr", "compat", "docVars", "rsids", "mathPr", "attachedSchema",
    "themeFontLang", "clrSchemeMapping", "doNotIncludeSubdocsInStats", "doNotAutoCompressPictures", "forceUpgrade",
    "captions", "readModeInkLockDown", "smartTagType", "schemaLibrary", "shapeDefaults",
    "doNotEmbedSmartTags", "decimalSymbol", "listSeparator",
)


def field_instructions(document: etree._Element) -> list[str]:
    """The instruction text of every field of the document body, in order."""
    return [(node.text or "").strip() for node in document.iter(w("instrText")) if (node.text or "").strip()]


def request_field_update(package: OpcPackage) -> bool:
    """Set ``w:updateFields`` in ``word/settings.xml``; False when the package has no settings part."""
    if not package.has("word/settings.xml"):
        return False
    settings = package.xml("word/settings.xml")
    existing = settings.find(w("updateFields"))
    if existing is None:
        existing = etree.Element(w("updateFields"))
        following = next((c for c in settings if etree.QName(c).localname in _AFTER_UPDATE_FIELDS), None)
        if following is not None:
            following.addprevious(existing)
        else:
            settings.append(existing)
    existing.set(w("val"), "true")
    return True


# CT_Compat children that precede w:doNotExpandShiftReturn in the schema sequence
_BEFORE_SHIFT_RETURN = (
    "useSingleBorderforContiguousCells", "wpJustification", "noTabHangInd", "noLeading", "spaceForUL",
    "noColumnBalance", "balanceSingleByteDoubleByteWidth", "noExtraLineSpacing", "doNotLeaveBackslashAlone",
    "ulTrailSpace",
)


def keep_line_breaks_unstretched(package: OpcPackage) -> bool:
    """``w:compat/w:doNotExpandShiftReturn``: a justified line ended by a manual break (LaTeX ``\\\\``) is not
    stretched to the margin, as in TeX. False when the package has no settings part."""
    if not package.has("word/settings.xml"):
        return False
    settings = package.xml("word/settings.xml")
    compat = settings.find(w("compat"))
    if compat is None:
        compat = etree.Element(w("compat"))
        following = next((c for c in settings if etree.QName(c).localname in _AFTER_UPDATE_FIELDS[3:]), None)
        if following is not None:
            following.addprevious(compat)
        else:
            settings.append(compat)
    if compat.find(w("doNotExpandShiftReturn")) is None:
        flag = etree.Element(w("doNotExpandShiftReturn"))
        following = next((c for c in compat if etree.QName(c).localname not in _BEFORE_SHIFT_RETURN), None)
        if following is not None:
            following.addprevious(flag)
        else:
            compat.append(flag)
    return True


__all__ = ["field_instructions", "keep_line_breaks_unstretched", "request_field_update"]
