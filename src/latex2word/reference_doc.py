"""The Pandoc ``--reference-doc``: page geometry and named styles of the publisher layout.

Pandoc copies the page setup (``w:sectPr``) and every style it finds in the
reference document into its output, so the profile's page and typography rules
reach the DOCX through this one file.

* ``generated``: Pandoc's own default reference (read from ``pandoc
  --print-default-data-file``; the installed data is never touched) with an
  empty body carrying only the profile's section properties, and its styles
  patched from the profile. Same Pandoc version + profile = same bytes.
* ``external``: a publisher template, read-only. Styles the layout code needs
  are *added* when missing; existing styles are never modified.

Diagnostics: ``L2W-E-REFDOC-001`` the external file is not a readable DOCX with
``word/styles.xml``; ``-002`` no external path; ``-003`` Pandoc's default
reference is unavailable; ``-004`` the profile has no resolved layout.
(``-005``/``-006`` come from :mod:`latex2word.ooxml.styles`.)
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lxml import etree

from latex2word.errors import BuildError
from latex2word.layout import ResolvedLayout
from latex2word.ooxml.namespaces import w
from latex2word.ooxml.package import OpcPackage, atomic_write, parse_xml, serialize_xml
from latex2word.ooxml.sections import reference_section_properties
from latex2word.ooxml.styles import apply_typography, ensure_required_styles
from latex2word.pandoc_adapter import PandocTool
from latex2word.profiles import Profile

STYLES_PART = "word/styles.xml"
DOCUMENT_PART = "word/document.xml"
CORE_PART = "docProps/core.xml"
REFERENCE_NAME = "reference.docx"
_DCTERMS = "http://purl.org/dc/terms/"


@dataclass(frozen=True)
class ReferenceDoc:
    """The reference document written for a build."""

    path: Path
    sha256: str
    mode: str  # generated | external
    source_sha256: str  # Pandoc's default reference.docx, or the external template file
    pandoc_version: str
    created_styles: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "mode": self.mode,
            "source_sha256": self.source_sha256,
            "pandoc_version": self.pandoc_version,
            "created_styles": list(self.created_styles),
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixed_timestamp() -> str:
    """W3CDTF instant of ``SOURCE_DATE_EPOCH`` (1970-01-01T00:00:00Z when unset or malformed)."""
    raw = os.environ.get("SOURCE_DATE_EPOCH", "")
    seconds = int(raw) if raw.isdigit() else 0
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pandoc_default_reference(pandoc: PandocTool, timeout_s: float) -> bytes:
    """Pandoc's bundled ``reference.docx`` (binary on stdout; user data directories are ignored by this command)."""
    argv = [pandoc.path, "--print-default-data-file", "reference.docx"]
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout_s, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildError(f"L2W-E-REFDOC-003: cannot obtain Pandoc's default reference.docx: {exc}") from exc
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", errors="replace").strip() or f"exit code {completed.returncode}"
        raise BuildError(f"L2W-E-REFDOC-003: pandoc could not print its default reference.docx: {detail}")
    return completed.stdout


def _empty_body(package: OpcPackage, layout: ResolvedLayout) -> None:
    """Replace the sample body by one that only carries the section properties (root and namespaces are kept)."""
    document = package.xml(DOCUMENT_PART)
    document.text = None
    for child in list(document):
        document.remove(child)
    etree.SubElement(document, w("body")).append(reference_section_properties(layout))


def _fix_core_timestamps(package: OpcPackage) -> None:
    if not package.has(CORE_PART):
        return
    core = package.xml(CORE_PART)
    stamp = _fixed_timestamp()
    for name in ("created", "modified"):
        for element in core.iter(f"{{{_DCTERMS}}}{name}"):
            element.text = stamp


def _generated(layout: ResolvedLayout, pandoc: PandocTool, timeout_s: float) -> tuple[bytes, str, tuple[str, ...]]:
    source = _pandoc_default_reference(pandoc, timeout_s)
    package = OpcPackage.load(source)
    created = apply_typography(package.xml(STYLES_PART), layout)
    _empty_body(package, layout)
    _fix_core_timestamps(package)
    return package.to_bytes(), _sha256(source), tuple(created)


def _read_external(path: Path) -> tuple[bytes, OpcPackage, etree._Element]:
    """The template bytes, its package and a detached tree of its styles part (the package itself stays unparsed)."""
    try:
        source = path.read_bytes()
    except OSError as exc:
        raise BuildError(f"L2W-E-REFDOC-001: cannot read the external reference document {path.name!r}: {exc}") from exc
    try:
        package = OpcPackage.load(source)
        if not package.has(STYLES_PART):
            raise BuildError(f"the package has no {STYLES_PART}")
        return source, package, parse_xml(package.read(STYLES_PART))
    except (BuildError, etree.XMLSyntaxError) as exc:
        raise BuildError(f"L2W-E-REFDOC-001: {path.name!r} is not a usable DOCX reference document: {exc}") from exc


def _external(layout: ResolvedLayout, path: Path) -> tuple[bytes, str, tuple[str, ...]]:
    source, package, styles = _read_external(path)
    created = ensure_required_styles(styles, layout)
    if not created:  # nothing missing: the template is copied byte for byte
        return source, _sha256(source), ()
    package.set_bytes(STYLES_PART, serialize_xml(styles))
    return package.to_bytes(), _sha256(source), tuple(created)


def build_reference_docx(profile: Profile, pandoc: PandocTool, workdir: Path, *, timeout_s: float = 60.0) -> ReferenceDoc:
    """Write ``<workdir>/reference.docx`` for ``profile`` and describe it."""
    layout = profile.layout
    if layout is None:
        raise BuildError(f"L2W-E-REFDOC-004: profile {profile.id!r} has no resolved layout")
    mode = profile.reference_docx.mode if profile.reference_docx is not None else "generated"
    if mode == "external":
        if profile.reference_docx_path is None:
            raise BuildError(f"L2W-E-REFDOC-002: profile {profile.id!r} selects an external reference document without a path")
        data, source_sha256, created = _external(layout, profile.reference_docx_path)
    else:
        data, source_sha256, created = _generated(layout, pandoc, timeout_s)
    destination = workdir / REFERENCE_NAME
    atomic_write(destination, data)
    return ReferenceDoc(destination, _sha256(data), mode, source_sha256, pandoc.version, created)


__all__ = ["ReferenceDoc", "build_reference_docx"]
