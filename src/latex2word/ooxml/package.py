"""An OPC (zip) package held in memory, edited as XML trees, written deterministically.

One :class:`OpcPackage` is loaded per finalisation. Parts are parsed lazily with
lxml (which keeps every namespace declaration and prefix exactly as written, so
``mc:Ignorable`` lists in externally authored templates stay valid), mutated in
place and serialised once by :meth:`to_bytes`. The archive is written with fixed
timestamps so the same logical content gives the same bytes.
"""

from __future__ import annotations

import io
import os
import posixpath
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

from latex2word.errors import BuildError
from latex2word.ooxml.namespaces import NS

CONTENT_TYPES = "[Content_Types].xml"
_FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def xml_parser() -> etree.XMLParser:
    """A parser that never resolves entities or touches the network."""
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def parse_xml(data: bytes) -> etree._Element:
    return etree.fromstring(data, xml_parser())


def serialize_xml(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _zip_date() -> tuple[int, int, int, int, int, int]:
    """Entry timestamp: ``SOURCE_DATE_EPOCH`` (clamped to the zip minimum) or the fixed 1980 date."""
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw and raw.isdigit():
        import time

        stamp = time.gmtime(max(int(raw), 315532800))
        return (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, stamp.tm_hour, stamp.tm_min, stamp.tm_sec - stamp.tm_sec % 2)
    return _FIXED_DATE


class OpcPackage:
    """Parts by name, in archive order."""

    def __init__(self, parts: dict[str, bytes]) -> None:
        self._parts: dict[str, bytes] = dict(parts)
        self._trees: dict[str, etree._Element] = {}

    # -- loading ---------------------------------------------------------------------------------
    @classmethod
    def load(cls, source: Path | bytes) -> OpcPackage:
        try:
            archive = zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else source)
        except (zipfile.BadZipFile, OSError) as exc:
            raise BuildError(f"not a readable OPC package: {exc}") from exc
        with archive:
            names = [i.filename for i in archive.infolist() if not i.filename.endswith("/")]
            duplicates = sorted(n for n, c in Counter(names).items() if c > 1)
            if duplicates:
                raise BuildError(f"the package has duplicate entries: {', '.join(duplicates)}")
            return cls({name: archive.read(name) for name in names})

    # -- parts -------------------------------------------------------------------------------------
    @property
    def names(self) -> list[str]:
        return list(self._parts)

    def has(self, name: str) -> bool:
        return name in self._parts

    def read(self, name: str) -> bytes:
        """Current bytes of ``name`` (serialising the edited tree when the part was parsed)."""
        if name in self._trees:
            return serialize_xml(self._trees[name])
        try:
            return self._parts[name]
        except KeyError:
            raise BuildError(f"the package has no part {name!r}") from None

    def xml(self, name: str) -> etree._Element:
        """The parsed root of an XML part; later calls return the same (mutable) tree."""
        if name not in self._trees:
            try:
                self._trees[name] = parse_xml(self._parts[name])
            except KeyError:
                raise BuildError(f"the package has no part {name!r}") from None
            except etree.XMLSyntaxError as exc:
                raise BuildError(f"part {name!r} is not well-formed XML: {exc}") from exc
        return self._trees[name]

    def set_bytes(self, name: str, data: bytes) -> None:
        self._trees.pop(name, None)
        self._parts[name] = data

    def remove(self, name: str) -> None:
        self._trees.pop(name, None)
        self._parts.pop(name, None)

    # -- relationships -------------------------------------------------------------------------------
    def relationship_targets(self, part: str) -> dict[str, str]:
        """``Id -> resolved internal part name`` of the relationships of ``part`` (external ones skipped)."""
        directory, base = posixpath.split(part)
        rels_name = posixpath.join(directory, "_rels", f"{base}.rels")
        if rels_name not in self._parts:
            return {}
        out: dict[str, str] = {}
        for rel in self.xml(rels_name).iter(f"{{{NS['pkg']}}}Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            target = unquote(rel.get("Target", "").split("#", 1)[0])
            resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(directory, target))
            out[rel.get("Id", "")] = resolved
        return out

    # -- writing -------------------------------------------------------------------------------------
    def to_bytes(self) -> bytes:
        """The package as a zip: ``[Content_Types].xml`` first, then archive order, fixed timestamps."""
        names = sorted(self._parts, key=lambda n: n != CONTENT_TYPES)  # stable sort: only the content types move
        stamp = _zip_date()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name in names:
                info = zipfile.ZipInfo(name, stamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, self.read(name))
        return buffer.getvalue()

    def write_atomic(self, destination: Path) -> bytes:
        """Write to a temporary sibling, then replace ``destination``; returns the written bytes."""
        data = self.to_bytes()
        atomic_write(destination, data)
        return data


def atomic_write(destination: Path, data: bytes) -> None:
    """Replace ``destination`` with ``data`` so readers see the old file or the new one, never a partial one."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
