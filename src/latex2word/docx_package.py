"""Defensive reading of a DOCX/OPC package that is treated as untrusted input.

``latex2word validate-docx`` and the snapshot subsystem accept arbitrary files. Before any XML is interpreted
:func:`read_package` enforces, on the ZIP container and on the OPC structure:

* member names: no absolute paths, drive letters, backslashes, ``..`` segments, NUL bytes, duplicates or
  encrypted entries;
* resource limits: member count, per-member and total *actual* uncompressed size (counted while inflating, the
  header sizes are never trusted) and the compression ratio of large members (ZIP bombs);
* XML safety: no DTD and no entity declarations (entity expansion attacks are rejected, never expanded);
* relationships: internal targets must stay inside the package, external targets are only accepted for
  hyperlinks with a plain web/mail scheme (an external image, template, OLE object or ``file:`` target is
  something the latex2word pipeline never writes, so it is reported);
* macros: macro-enabled content types and ``vbaProject`` parts are reported - macros are never executed and
  macro-enabled documents are outside the output target;
* the package root: ``_rels/.rels`` must name ``word/document.xml`` as the office document.

Nothing outside the archive is ever opened and no network resource is ever fetched. Every problem is a
:class:`~latex2word.source_graph.Finding`; the functions never raise for damaged input.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit

from lxml import etree

from latex2word.diagnostics import Severity
from latex2word.ooxml.package import parse_xml
from latex2word.source_graph import Finding

CONTENT_TYPES = "[Content_Types].xml"
MAIN_DOCUMENT = "word/document.xml"
ROOT_RELS = "_rels/.rels"

_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
OFFICE_DOCUMENT_REL = f"{_REL_BASE}/officeDocument"
HYPERLINK_REL = f"{_REL_BASE}/hyperlink"

MAIN_DOCUMENT_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_MACRO_MARKERS = ("macroenabled", "vbaproject")
_ALLOWED_EXTERNAL_SCHEMES = frozenset({"http", "https", "mailto", "ftp"})
_RESERVED_DEVICE = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)
_DTD = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)


@dataclass(frozen=True)
class PackageLimits:
    """Upper bounds for one package. The defaults are far above any real manuscript and far below a bomb."""

    max_members: int = 4096
    max_member_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    max_ratio: float = 250.0  # uncompressed / compressed, applied to members above ``ratio_floor_bytes``
    ratio_floor_bytes: int = 1024 * 1024
    max_xml_bytes: int = 128 * 1024 * 1024


DEFAULT_LIMITS = PackageLimits()


@dataclass
class PackageRead:
    """Outcome of :func:`read_package`."""

    parts: dict[str, bytes] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    readable: bool = False  # False: the container itself was rejected (no part is trustworthy)

    @property
    def ok(self) -> bool:
        return self.readable and not any(f.severity is Severity.ERROR for f in self.findings)


def _error(code: str, message: str) -> Finding:
    return Finding(code, Severity.ERROR, message)


def _warning(code: str, message: str) -> Finding:
    return Finding(code, Severity.WARNING, message)


def unsafe_member_reason(name: str) -> str | None:
    """Why a ZIP member name must not be trusted (``None``: acceptable). Pure; independent of the host OS."""
    if "\x00" in name:
        return "contains a NUL byte"
    if name.startswith(("/", "\\")):
        return "is an absolute path"
    if "\\" in name:
        return "contains a backslash"
    windows = PureWindowsPath(name)
    if windows.drive or windows.root:
        return "carries a drive letter or root"
    parts = PurePosixPath(name).parts
    if any(part == ".." for part in parts):
        return "contains a '..' segment"
    for part in parts:
        if part.endswith((".", " ")) and part not in (".", ".."):
            return f"has a segment with a trailing dot or space ({part!r})"
        if _RESERVED_DEVICE.match(part):
            return f"uses the reserved device name {part!r}"
    return None


def _inflate(archive: zipfile.ZipFile, info: zipfile.ZipInfo, cap: int) -> bytes | str:
    """The decompressed bytes of one member, or a reason string. Counts real bytes, never trusts the header."""
    chunks: list[bytes] = []
    total = 0
    try:
        with archive.open(info) as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    return f"inflates beyond the {cap} byte limit"
                chunks.append(chunk)
    except (zipfile.BadZipFile, OSError, NotImplementedError, RuntimeError, EOFError, ValueError) as exc:
        return f"cannot be read: {exc}"
    return b"".join(chunks)


def read_package(path: Path, limits: PackageLimits = DEFAULT_LIMITS) -> PackageRead:
    """Read every member of the DOCX at ``path`` into memory, enforcing the safety rules of the module docstring."""
    result = PackageRead()
    findings = result.findings
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        findings.append(_error("L2W-E-DOCX-001", f"{path.name} is not a readable ZIP/DOCX package: {exc}"))
        return result
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_members:
            findings.append(_error("L2W-E-PKG-002", f"the package has {len(infos)} members, more than the limit {limits.max_members}"))
            return result
        names = [i.filename for i in infos]
        for name, count in Counter(names).items():
            if count > 1:
                findings.append(_error("L2W-E-DOCX-001", f"duplicate ZIP member {name!r} ({count} entries)"))
        rejected = False
        for info in infos:
            reason = unsafe_member_reason(info.filename)
            if reason is not None:
                findings.append(_error("L2W-E-PKG-001", f"unsafe ZIP member name {info.filename!r}: it {reason}"))
                rejected = True
            if info.flag_bits & 0x1:
                findings.append(_error("L2W-E-PKG-003", f"ZIP member {info.filename!r} is encrypted"))
                rejected = True
        declared = sum(i.file_size for i in infos)
        if declared > limits.max_total_bytes:
            findings.append(_error("L2W-E-PKG-002", f"the package declares {declared} uncompressed bytes, more than the limit {limits.max_total_bytes}"))
            rejected = True
        if rejected or any(f.code == "L2W-E-DOCX-001" for f in findings):
            return result
        seen: set[str] = set()
        total = 0
        for info in infos:
            if info.filename.endswith("/") or info.filename in seen:
                continue
            seen.add(info.filename)
            if info.file_size > limits.ratio_floor_bytes and info.compress_size > 0 and info.file_size / info.compress_size > limits.max_ratio:
                findings.append(
                    _error("L2W-E-PKG-002", f"ZIP member {info.filename!r} has a compression ratio of "
                                            f"{info.file_size / info.compress_size:.0f}:1 (limit {limits.max_ratio:.0f}:1): possible ZIP bomb")
                )
                return result
            data = _inflate(archive, info, limits.max_member_bytes)
            if isinstance(data, str):
                findings.append(_error("L2W-E-PKG-002" if "limit" in data else "L2W-E-DOCX-002", f"ZIP member {info.filename!r} {data}"))
                if "limit" in data:
                    return result
                continue
            total += len(data)
            if total > limits.max_total_bytes:
                findings.append(_error("L2W-E-PKG-002", f"the package inflates beyond the {limits.max_total_bytes} byte limit"))
                return result
            result.parts[info.filename] = data
    result.readable = True
    return result


# --------------------------------------------------------------------------- OPC structure


def _is_xml_part(name: str) -> bool:
    return name.endswith((".xml", ".rels"))


def check_xml_safety(parts: dict[str, bytes], findings: list[Finding]) -> None:
    """Reject DTDs/entity declarations (never expanded by our parser, but an input that has them is hostile)."""
    for name, data in parts.items():
        if not _is_xml_part(name):
            continue
        if len(data) > DEFAULT_LIMITS.max_xml_bytes:
            findings.append(_error("L2W-E-PKG-002", f"XML part {name!r} is larger than the {DEFAULT_LIMITS.max_xml_bytes} byte limit"))
        elif _DTD.search(data):
            findings.append(_error("L2W-E-PKG-009", f"XML part {name!r} declares a DTD or entities, which OOXML never needs"))


def _rels_owner(rels_name: str) -> str:
    directory, base = posixpath.split(rels_name)
    return posixpath.join(posixpath.dirname(directory), base[: -len(".rels")]) if base != ".rels" else ""


def _safe_internal_target(owner: str, target: str) -> tuple[str | None, str | None]:
    """``(resolved part name, None)`` or ``(None, reason)`` for an internal relationship target."""
    raw = unquote(target.split("#", 1)[0])
    if not raw:
        return None, "is empty"
    if "\\" in raw or PureWindowsPath(raw).drive or raw.startswith("//") or "\x00" in raw:
        return None, "is a local filesystem path"
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", raw):
        return None, "uses a URI scheme but is not marked External"
    base = "" if raw.startswith("/") else posixpath.dirname(owner)
    joined = posixpath.normpath(posixpath.join(base, raw.lstrip("/")))
    if joined == ".." or joined.startswith("../") or joined.startswith("/"):
        return None, "leaves the package"
    return joined, None


def check_relationships(parts: dict[str, bytes], findings: list[Finding]) -> dict[str, list[dict[str, str]]]:
    """Validate every relationships part; returns ``owner -> [{id, type, target, mode}]`` of the readable ones."""
    out: dict[str, list[dict[str, str]]] = {}
    for name in sorted(parts):
        if not name.endswith(".rels"):
            continue
        try:
            root = parse_xml(parts[name])
        except etree.XMLSyntaxError:
            continue  # reported as malformed XML by the caller
        owner = _rels_owner(name)
        rows: list[dict[str, str]] = []
        for rel in root.iter(f"{{{_PKG_REL}}}Relationship"):
            rel_type, target, mode, rel_id = rel.get("Type", ""), rel.get("Target", ""), rel.get("TargetMode", "Internal"), rel.get("Id", "")
            rows.append({"id": rel_id, "type": rel_type, "target": target, "mode": mode})
            if mode == "External":
                scheme = urlsplit(target).scheme.lower()
                if rel_type != HYPERLINK_REL:
                    findings.append(
                        _error("L2W-E-PKG-005", f"external relationship {rel_id!r} in {name!r} has type {rel_type.rsplit('/', 1)[-1]!r} "
                                                f"(only hyperlinks may be external): {target[:80]!r}")
                    )
                elif scheme not in _ALLOWED_EXTERNAL_SCHEMES:
                    findings.append(
                        _error("L2W-E-PKG-005", f"external hyperlink {rel_id!r} in {name!r} uses the scheme {scheme or '(none)'!r}: {target[:80]!r}")
                    )
                continue
            resolved, reason = _safe_internal_target(owner, target)
            if reason is not None:
                findings.append(_error("L2W-E-PKG-004", f"relationship {rel_id!r} in {name!r} targets {target[:80]!r}, which {reason}"))
        out[owner] = rows
    return out


def check_content_types(parts: dict[str, bytes], findings: list[Finding]) -> None:
    """The content-types part must exist, declare the main document correctly and carry no macro types."""
    data = parts.get(CONTENT_TYPES)
    if data is None:
        return  # the missing part is reported as a required-part problem by the caller
    try:
        root = parse_xml(data)
    except etree.XMLSyntaxError:
        return
    overrides = {o.get("PartName", "").lstrip("/"): o.get("ContentType", "") for o in root.iter(f"{{{_CT}}}Override")}
    defaults = {d.get("Extension", "").lower(): d.get("ContentType", "") for d in root.iter(f"{{{_CT}}}Default")}
    declared = [*overrides.values(), *defaults.values()]
    if any(marker in value.lower() for value in declared for marker in _MACRO_MARKERS):
        findings.append(_error("L2W-E-PKG-007", "the package declares a macro-enabled content type; macro-enabled documents are not supported"))
    if MAIN_DOCUMENT in parts:
        ctype = overrides.get(MAIN_DOCUMENT) or defaults.get("xml")
        if ctype != MAIN_DOCUMENT_CONTENT_TYPE:
            findings.append(_error("L2W-E-PKG-006", f"{MAIN_DOCUMENT} has content type {ctype!r}, expected the WordprocessingML main document type"))
    for name in parts:
        if name.lower().endswith(("vbaproject.bin", "vbadata.xml")) or "/vba" in name.lower():
            findings.append(_error("L2W-E-PKG-007", f"the package contains the macro part {name!r}; macros are never executed"))
            break


def check_package_root(parts: dict[str, bytes], rels: dict[str, list[dict[str, str]]], findings: list[Finding]) -> None:
    """``_rels/.rels`` must exist and name ``word/document.xml`` as the office document."""
    if ROOT_RELS not in parts:
        findings.append(_error("L2W-E-PKG-008", f"the package has no {ROOT_RELS}; it is not an OPC package"))
        return
    roots = rels.get("", [])
    targets = [posixpath.normpath(r["target"].lstrip("/")) for r in roots if r["type"] == OFFICE_DOCUMENT_REL and r["mode"] != "External"]
    if not targets:
        findings.append(_error("L2W-E-PKG-008", f"{ROOT_RELS} has no officeDocument relationship"))
    elif MAIN_DOCUMENT not in targets:
        findings.append(_error("L2W-E-PKG-008", f"the office document is {targets[0]!r}, not {MAIN_DOCUMENT!r}: not a WordprocessingML package"))


def check_package(parts: dict[str, bytes]) -> list[Finding]:
    """All OPC-level safety findings of an already-read package (see the module docstring)."""
    findings: list[Finding] = []
    check_xml_safety(parts, findings)
    rels = check_relationships(parts, findings)
    check_content_types(parts, findings)
    check_package_root(parts, rels, findings)
    return findings


def iter_findings(*groups: list[Finding]) -> Iterator[Finding]:
    """Findings of several groups without repeats (same code and message)."""
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for finding in group:
            key = (finding.code, finding.message)
            if key not in seen:
                seen.add(key)
                yield finding


def limits_to_dict(limits: PackageLimits = DEFAULT_LIMITS) -> dict[str, Any]:
    return {
        "max_members": limits.max_members, "max_member_bytes": limits.max_member_bytes,
        "max_total_bytes": limits.max_total_bytes, "max_ratio": limits.max_ratio,
        "ratio_floor_bytes": limits.ratio_floor_bytes, "max_xml_bytes": limits.max_xml_bytes,
    }
