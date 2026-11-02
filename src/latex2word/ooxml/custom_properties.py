"""Custom document properties (``docProps/custom.xml``) that describe the build machine.

Pandoc writes every simple metadata value as a custom property, including ``csl``: the path of the CSL file it was
given, i.e. an absolute path on the machine that ran the build. The bibliography is already rendered when the
finaliser runs, so the property carries no information about the document, only about the builder (privacy) and the
build location (reproducibility). It is removed by name; every other custom property is left as it is.
"""

from __future__ import annotations

from latex2word.ooxml.package import OpcPackage, parse_xml, serialize_xml

CUSTOM_PART = "docProps/custom.xml"
_CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
# Pandoc options whose metadata value is a path on the build machine.
BUILD_PATH_PROPERTIES = frozenset({"csl"})


def remove_build_path_properties(package: OpcPackage) -> list[str]:
    """Remove the custom properties in :data:`BUILD_PATH_PROPERTIES`; return the names removed."""
    if not package.has(CUSTOM_PART):
        return []
    # a private copy: the part is rewritten only when a property is really removed (otherwise byte-identical)
    root = parse_xml(package.read(CUSTOM_PART))
    removed: list[str] = []
    for prop in list(root.iter(f"{{{_CUSTOM_NS}}}property")):
        if prop.get("name") in BUILD_PATH_PROPERTIES:
            prop.getparent().remove(prop)
            removed.append(prop.get("name"))
    if removed:
        package.set_bytes(CUSTOM_PART, serialize_xml(root))
    return removed
