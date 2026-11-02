"""Structural and semantic validation of a generated DOCX package.

The validator never raises for a damaged package; every problem is reported as a
:class:`~latex2word.source_graph.Finding` (codes ``L2W-<E|W>-<AREA>-<nnn>``) and a
JSON-serialisable ``metrics`` mapping describes what was found. It checks, in order:

* OPC/XML integrity (ZIP members, XML well-formedness, relationships, content types);
* bookmarks, internal hyperlinks and unresolved ``??`` references;
* raw TeX / unresolved citation placeholders leaking into visible text;
* caption/equation/heading numbering against the filter report and the profile's formats;
* math structure (alignment ``&`` leaks, raw ``\\begin``/``\\end``);
* embedded figures (media hash, aspect ratio, consistent implied text width);
* bibliography paragraph count.

Assumptions about the Pandoc DOCX writer are listed in :func:`validate_docx`.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import statistics
import zipfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from lxml import etree

from latex2word.diagnostics import Severity
from latex2word.numerals import NumeralError, parse_numeral, template_regex
from latex2word.ooxml.package import parse_xml
from latex2word.ooxml.style_ids import LAYOUT_TABLE_STYLES
from latex2word.source_graph import Finding

EMU_PER_INCH = 914400

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_ASVG = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def _q(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


W_P, W_T, W_TAB, W_BR = _q(_W, "p"), _q(_W, "t"), _q(_W, "tab"), _q(_W, "br")
W_PSTYLE, W_PPR, W_VAL = _q(_W, "pStyle"), _q(_W, "pPr"), _q(_W, "val")
W_BOOKMARK, W_HYPERLINK, W_ANCHOR, W_TBL = (
    _q(_W, "bookmarkStart"),
    _q(_W, "hyperlink"),
    _q(_W, "anchor"),
    _q(_W, "tbl"),
)
W_TYPE = _q(_W, "type")
M_T, M_OMATH, M_OMATHPARA, M_EQARR, M_E = (
    _q(_M, "t"),
    _q(_M, "oMath"),
    _q(_M, "oMathPara"),
    _q(_M, "eqArr"),
    _q(_M, "e"),
)
R_ID, R_EMBED = _q(_R, "id"), _q(_R, "embed")
WP_INLINE, WP_ANCHOR, WP_EXTENT = _q(_WP, "inline"), _q(_WP, "anchor"), _q(_WP, "extent")
A_BLIP, ASVG_BLIP = _q(_A, "blip"), _q(_ASVG, "svgBlip")

_RAW_TEX_ERROR = re.compile(
    r"\\(?:cite[A-Za-z]*|ref|eqref|autoref|cref|label|input|include|begin|end)(?![A-Za-z])"
)
_ANY_TEX = re.compile(r"\\[A-Za-z]+")
_MATH_ENV = re.compile(r"\\(?:begin|end)(?![A-Za-z])")
_CITE_PLACEHOLDER = re.compile(r"\(\s*[\w:.\-]+\?\s*\)|\[\?\]|\[@[\w:.\-]+\]")
_HEADING_STYLE = re.compile(r"^Heading([1-6])(?:Unlisted)?$")  # Unlisted: starred heading kept out of the TOC

_SEQUENCE_KINDS = ("figures", "tables", "equations")
_CAPTION_STYLES = {"figures": "ImageCaption", "tables": "TableCaption"}


@dataclass(frozen=True)
class FigureExpectation:
    """What one figure of the source should look like in the DOCX."""

    id: str
    sha256: str | None
    aspect: float | None
    width_fraction: float | None
    width_inches: float | None
    requested: str | None = None


@dataclass(frozen=True)
class DocxExpectations:
    """Everything the validator compares the package against."""

    numbering: Mapping[str, Any]
    report: Mapping[str, Any]
    figures: Sequence[FigureExpectation] = ()
    bibliography_entries: int | None = None
    citations_expected: bool = False
    text_width_inches: float | None = None
    size_tolerance: float = 0.02


@dataclass
class DocxValidation:
    """Findings plus structural metrics of one validated DOCX."""

    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(f.severity is Severity.ERROR for f in self.findings)


@dataclass
class _Para:
    style: str
    text: str  # visible text (w:t, tab, br), math excluded
    math: list[str]  # m:t texts of math inside the paragraph
    display: bool  # contains m:oMathPara


@dataclass
class _Drawing:
    cx: int
    cy: int
    targets: list[str]  # resolved media part names (blip, svgBlip)
    svg: bool


def _finding(sev: str, area: str, nnn: int, message: str) -> Finding:
    severity = {"E": Severity.ERROR, "W": Severity.WARNING, "I": Severity.INFO}[sev]
    return Finding(f"L2W-{sev}-{area}-{nnn:03d}", severity, message, None)


def _excerpt(text: str, start: int, end: int, margin: int = 30) -> str:
    return text[max(0, start - margin) : end + margin].replace("\n", " ").strip()


def _is_xml_part(name: str) -> bool:
    return name.endswith((".xml", ".rels"))


def _resolve_target(source_part: str, target: str) -> str:
    target = unquote(target.split("#", 1)[0])
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def _rels_source(rels_name: str) -> str:
    directory, base = posixpath.split(rels_name)
    parent = posixpath.dirname(directory)
    return posixpath.join(parent, base[: -len(".rels")]) if base != ".rels" else ""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _collect_text(element: etree._Element, parts: list[str], math: list[str]) -> None:
    for child in element:
        tag = child.tag
        if tag in (M_OMATH, M_OMATHPARA):
            math.extend(t.text or "" for t in child.iter(M_T))
        elif tag == W_T:
            parts.append(child.text or "")
        elif tag == W_TAB:
            parts.append("\t")
        elif tag == W_BR:
            if child.get(W_TYPE) not in ("page", "column"):
                parts.append("\n")
        else:
            _collect_text(child, parts, math)


def _paragraph(p: etree._Element) -> _Para:
    style = ""
    ppr = p.find(W_PPR)
    if ppr is not None:
        pstyle = ppr.find(W_PSTYLE)
        if pstyle is not None:
            style = pstyle.get(W_VAL, "")
    parts: list[str] = []
    math: list[str] = []
    _collect_text(p, parts, math)
    # An equation paragraph carries its number either in the next paragraph (display math) or, in the tab-stop
    # layout, in the same paragraph after an inline equation; both count as "display" for numbering.
    display = p.find(f".//{M_OMATHPARA}") is not None or (style == "Equation" and p.find(f".//{M_OMATH}") is not None)
    return _Para(style, "".join(parts), math, display)


def _table_style(table: etree._Element) -> str:
    node = table.find(f"{_q(_W, 'tblPr')}/{_q(_W, 'tblStyle')}")
    return node.get(W_VAL, "") if node is not None else ""


def _norm(text: str) -> str:
    return " ".join(text.split())


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number_of(match: re.Match[str], styles: Mapping[str, str], preferred: Sequence[str]) -> int | None:
    groups = match.groupdict()
    for name in (*preferred, "arabic", "Roman", "roman", "Alpha", "alpha"):
        value = groups.get(name)
        if value is not None:
            style = name if name in ("arabic", "Roman", "roman", "Alpha", "alpha") else styles.get(name, "arabic")
            return parse_numeral(value, style)
    return None


def _field_styles(
    numbering: Mapping[str, Any], own: Mapping[str, Any], level: int | None = None, appendix: bool = False
) -> dict[str, str]:
    """Numeral style per template field; ``appendix``: the top-level counter uses the appendix numeral."""
    sections = _mapping(numbering.get("sections"))
    styles = {"n": str(own.get("numeral", "arabic"))}
    for i in (1, 2, 3):
        lv = _mapping(sections.get(f"level{i}"))
        styles[f"n{i}"] = str(lv.get("numeral", "arabic"))
    appendix_spec = _mapping(sections.get("appendix"))
    if appendix and appendix_spec:
        styles["n1"] = str(appendix_spec.get("numeral", "arabic"))
    if level is not None:
        # levels 4-6 have no {nK} template field of their own; their counter is {n}
        styles["n"] = styles.get(f"n{level}", str(own.get("numeral", "arabic")))
    return styles


def _has_appendix(numbering: Mapping[str, Any]) -> bool:
    return bool(_mapping(_mapping(numbering.get("sections")).get("appendix")))


def _enabled(kind_map: Mapping[str, Any]) -> bool:
    return bool(kind_map) and kind_map.get("enabled", True) is not False


def _is_strictly_increasing(values: Sequence[int]) -> bool:
    return all(b > a for a, b in zip(values, values[1:], strict=False))


def _load_xml(zf: zipfile.ZipFile, name: str, findings: list[Finding]) -> etree._Element | None:
    try:
        return parse_xml(zf.read(name))
    except (etree.XMLSyntaxError, KeyError, zipfile.BadZipFile, OSError, NotImplementedError, RuntimeError) as exc:
        findings.append(_finding("E", "DOCX", 2, f"XML part {name!r} cannot be parsed: {exc}"))
        return None


def _empty_metrics() -> dict[str, Any]:
    return {
        "parts": 0,
        "paragraphs": 0,
        "tables": 0,
        "layout_tables": 0,
        "drawings": 0,
        "svg_drawings": 0,
        "media": 0,
        "bookmarks": 0,
        "internal_hyperlinks": 0,
        "external_hyperlinks": 0,
        "omml_display": 0,
        "omml_inline": 0,
        "eqarr": 0,
        "bibliography_paragraphs": 0,
        "headings": 0,
        "numbering": {},
        "figures": [],
        "part_hashes": {},
    }


def validate_docx(path: Path, expectations: DocxExpectations) -> DocxValidation:
    """Validate the DOCX at ``path`` against ``expectations``.

    Never raises for damaged packages. Assumptions about the Pandoc writer: paragraph style ids
    ``Heading1..6``/``ImageCaption``/``TableCaption``/``Bibliography``; display equations are
    ``m:oMathPara`` inside a ``w:p`` whose label ``(n)`` is a plain ``w:t`` run; figure images are
    ``wp:inline`` drawings whose ``a:blip`` (PNG fallback) and ``asvg:svgBlip`` (SVG) embed ids resolve
    through ``word/_rels/document.xml.rels``. Numbering sequences are compared with
    ``report.sequences`` only for the kinds present in that report (a missing kind is not checked);
    ``reset`` policies other than ``none`` disable the duplicate/monotonic checks for that kind.
    """
    result = DocxValidation(metrics=_empty_metrics())
    findings = result.findings
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        findings.append(_finding("E", "DOCX", 1, f"{path.name} is not a readable ZIP/DOCX package: {exc}"))
        return result
    with zf:
        _validate_package(zf, expectations, result)
    return result


def _validate_package(zf: zipfile.ZipFile, ex: DocxExpectations, result: DocxValidation) -> None:
    findings, metrics = result.findings, result.metrics
    names = [i.filename for i in zf.infolist()]
    parts = [n for n in names if not n.endswith("/")]
    nameset = set(parts)
    metrics["parts"] = len(parts)

    for name, count in Counter(names).items():
        if count > 1:
            findings.append(_finding("E", "DOCX", 1, f"duplicate ZIP member {name!r} ({count} entries)"))
    for required in ("[Content_Types].xml", "word/document.xml"):
        if required not in nameset:
            findings.append(_finding("E", "DOCX", 1, f"required part {required!r} is missing from the package"))

    def read(name: str) -> bytes | None:
        try:
            return zf.read(name)
        except (KeyError, zipfile.BadZipFile, OSError, NotImplementedError, RuntimeError) as exc:
            findings.append(_finding("E", "DOCX", 2, f"part {name!r} cannot be read: {exc}"))
            return None

    trees: dict[str, etree._Element] = {}
    for name in parts:
        if _is_xml_part(name):
            data = read(name)
            if data is None:
                continue
            try:
                trees[name] = parse_xml(data)
            except etree.XMLSyntaxError as exc:
                findings.append(_finding("E", "DOCX", 2, f"XML part {name!r} is not well-formed: {exc}"))

    # relationships ---------------------------------------------------------------------------
    rel_targets: dict[str, dict[str, str]] = {}  # rels part -> id -> resolved target
    for name, tree in trees.items():
        if not name.endswith(".rels"):
            continue
        source = _rels_source(name)
        ids: dict[str, str] = {}
        for rel in tree.iter(_q(_PKG_REL, "Relationship")):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            resolved = _resolve_target(source, target)
            ids[rel.get("Id", "")] = resolved
            if resolved not in nameset:
                findings.append(
                    _finding(
                        "E",
                        "DOCX",
                        3,
                        f"relationship {rel.get('Id')!r} in {name!r} targets {target!r}, "
                        f"but part {resolved!r} does not exist",
                    )
                )
        rel_targets[name] = ids

    # content types ---------------------------------------------------------------------------
    ct = trees.get("[Content_Types].xml")
    if ct is not None:
        defaults = {d.get("Extension", "").lower() for d in ct.iter(_q(_CT, "Default"))}
        overrides = {o.get("PartName", "").lstrip("/") for o in ct.iter(_q(_CT, "Override"))}
        for name in parts:
            if name == "[Content_Types].xml":
                continue
            ext = name.rsplit(".", 1)[1].lower() if "." in posixpath.basename(name) else ""
            if name not in overrides and ext not in defaults:
                findings.append(_finding("W", "DOCX", 4, f"part {name!r} has no content type (extension {ext!r})"))

    # media + hashes ---------------------------------------------------------------------------
    media = [n for n in parts if n.startswith("word/media/")]
    metrics["media"] = len(media)
    hashes: dict[str, str] = {}
    for name in ("word/document.xml", "word/styles.xml", "word/numbering.xml", "word/_rels/document.xml.rels", *media):
        if name in nameset:
            data = read(name)
            if data is not None:
                hashes[name] = _sha(data)
    metrics["part_hashes"] = hashes

    document = trees.get("word/document.xml")
    if document is None:
        return
    doc_rels = rel_targets.get("word/_rels/document.xml.rels", {})

    paragraphs = [_paragraph(p) for p in document.iter(W_P)]
    metrics["paragraphs"] = len(paragraphs)
    layout_tables = sum(1 for t in document.iter(W_TBL) if _table_style(t) in LAYOUT_TABLE_STYLES)
    metrics["layout_tables"] = layout_tables  # borderless tables the layout pass adds (equations, author block)
    metrics["tables"] = sum(1 for _ in document.iter(W_TBL)) - layout_tables
    headings = [p for p in paragraphs if _HEADING_STYLE.match(p.style)]
    metrics["headings"] = len(headings)

    _check_links(document, metrics, findings)
    _check_text(paragraphs, findings)
    _check_numbering(paragraphs, headings, ex, result)
    _check_math(document, paragraphs, ex, result)
    _check_figures(document, doc_rels, hashes, ex, result)

    bib = sum(1 for p in paragraphs if p.style == "Bibliography")
    metrics["bibliography_paragraphs"] = bib
    if ex.bibliography_entries is not None and bib != ex.bibliography_entries:
        findings.append(
            _finding("E", "BIB", 10, f"bibliography has {bib} paragraph(s), expected {ex.bibliography_entries}")
        )
    if ex.citations_expected and bib == 0 and ex.bibliography_entries != 0:
        findings.append(_finding("E", "BIB", 11, "citations exist in the source but the document has no bibliography"))


def _check_links(document: etree._Element, metrics: dict[str, Any], findings: list[Finding]) -> None:
    bookmarks = [b.get(_q(_W, "name"), "") for b in document.iter(W_BOOKMARK)]
    metrics["bookmarks"] = len(bookmarks)
    for name, count in Counter(bookmarks).items():
        if count > 1:
            findings.append(_finding("E", "REF", 12, f"duplicate bookmark name {name!r} ({count} occurrences)"))
    known = set(bookmarks)
    internal = external = 0
    missing: list[str] = []
    for link in document.iter(W_HYPERLINK):
        anchor = link.get(W_ANCHOR)
        if anchor is not None:
            internal += 1
            if anchor not in known and anchor not in missing:
                missing.append(anchor)
        elif link.get(R_ID) is not None:
            external += 1
    metrics["internal_hyperlinks"] = internal
    metrics["external_hyperlinks"] = external
    if missing:
        findings.append(
            _finding("E", "REF", 10, f"{len(missing)} hyperlink anchor(s) have no bookmark: {', '.join(missing)}")
        )


def _check_text(paragraphs: Sequence[_Para], findings: list[Finding]) -> None:
    for index, p in enumerate(paragraphs, start=1):
        where = f"paragraph {index}"
        for m in re.finditer(r"\?\?", p.text):
            findings.append(
                _finding("E", "REF", 11, f"unresolved '??' in {where}: {_excerpt(p.text, m.start(), m.end())!r}")
            )
            break
        for m in _RAW_TEX_ERROR.finditer(p.text):
            findings.append(
                _finding(
                    "E", "TEX", 10, f"raw TeX {m.group(0)!r} in text of {where}: {_excerpt(p.text, m.start(), m.end())!r}"
                )
            )
        for m in _ANY_TEX.finditer(p.text):
            if not _RAW_TEX_ERROR.fullmatch(m.group(0)):
                findings.append(
                    _finding(
                        "W",
                        "TEX",
                        11,
                        f"TeX command {m.group(0)!r} in text of {where}: {_excerpt(p.text, m.start(), m.end())!r}",
                    )
                )
        for m in _CITE_PLACEHOLDER.finditer(p.text):
            findings.append(
                _finding("E", "CITE", 10, f"unresolved citation placeholder {m.group(0)!r} in {where}")
            )
        math_text = "".join(p.math)
        for m in _RAW_TEX_ERROR.finditer(math_text):
            findings.append(
                _finding(
                    "E",
                    "TEX",
                    10,
                    f"raw TeX {m.group(0)!r} in math of {where}: {_excerpt(math_text, m.start(), m.end())!r}",
                )
            )


def _compile(
    template: Any, styles: Mapping[str, str], what: str, findings: list[Finding]
) -> re.Pattern[str] | None:
    if not isinstance(template, str) or not template:
        return None
    try:
        return template_regex(template, styles)
    except (KeyError, NumeralError, re.error, ValueError) as exc:
        findings.append(_finding("E", "NUM", 14, f"{what} format {template!r} cannot be compiled: {exc}"))
        return None


def _check_sequence(
    kind: str,
    numbers: list[int],
    texts: list[str],
    expected: Sequence[Any] | None,
    reset_none: bool,
    findings: list[Finding],
) -> None:
    if reset_none:
        for text, count in Counter(texts).items():
            if count > 1:
                findings.append(_finding("E", "NUM", 10, f"duplicate {kind} number {text!r} ({count} occurrences)"))
        if not _is_strictly_increasing(numbers):
            findings.append(
                _finding("E", "NUM", 11, f"{kind} numbers are not strictly increasing: {numbers} ({texts})")
            )
    if expected is not None and list(expected) != numbers:
        findings.append(
            _finding("E", "NUM", 12, f"{kind} numbering in DOCX {numbers} differs from the filter report {list(expected)}")
        )


def _check_numbering(
    paragraphs: Sequence[_Para], headings: Sequence[_Para], ex: DocxExpectations, result: DocxValidation
) -> None:
    findings, extracted = result.findings, result.metrics["numbering"]
    numbering = ex.numbering
    sequences = _mapping(_mapping(ex.report).get("sequences"))

    for kind in _SEQUENCE_KINDS:
        kmap = _mapping(numbering.get(kind))
        if not _enabled(kmap):
            continue
        regex = _compile(kmap.get("format"), _field_styles(numbering, kmap), kind, findings)
        if regex is None:
            continue
        # after \appendix the top-level field ({n1}) may use another numeral style (A.1 instead of 6.1)
        variants: list[tuple[re.Pattern[str], dict[str, str]]] = [(regex, _field_styles(numbering, kmap))]
        if _has_appendix(numbering):
            appendix_styles = _field_styles(numbering, kmap, appendix=True)
            if appendix_styles != variants[0][1]:
                appendix_regex = _compile(kmap.get("format"), appendix_styles, kind, findings)
                if appendix_regex is not None:
                    variants.append((appendix_regex, appendix_styles))
        numbers: list[int] = []
        texts: list[str] = []
        consumed: set[int] = set()
        for idx, p in enumerate(paragraphs):
            if idx in consumed:
                continue
            m: re.Match[str] | None = None
            styles = variants[0][1]
            for regex, styles in variants:
                if kind in _CAPTION_STYLES:
                    if p.style != _CAPTION_STYLES[kind]:
                        break
                    m = regex.match(p.text.lstrip())
                elif p.style == "EquationNumber":
                    m = regex.search(p.text)
                elif p.display:
                    for m in regex.finditer(p.text):
                        pass
                    nxt = paragraphs[idx + 1] if idx + 1 < len(paragraphs) else None
                    if m is None and nxt is not None and nxt.style != "EquationNumber" and not nxt.display:
                        m = regex.fullmatch(nxt.text.strip())
                        if m is not None:
                            consumed.add(idx + 1)
                if m is not None:
                    break
            if m is None:
                continue
            n = _number_of(m, styles, ("n",))
            if n is None:
                continue
            numbers.append(n)
            texts.append(_norm(m.group(0)))
        extracted[kind] = {"numbers": numbers, "texts": texts}
        expected = None
        if kind in sequences:
            expected = [e.get("n") for e in sequences[kind] if isinstance(e, Mapping)]
        reset_none = str(kmap.get("reset", "none")) == "none"
        _check_sequence(kind, numbers, texts, expected, reset_none, findings)

    smap = _mapping(numbering.get("sections"))
    if not _enabled(smap):
        return
    regexes: dict[int, re.Pattern[str]] = {}
    appendix_regexes: dict[int, re.Pattern[str]] = {}  # deeper levels inside the appendix ({n1} = A, B, ...)
    for level in range(1, 7):
        lv = _mapping(smap.get(f"level{level}"))
        if not lv:
            continue
        regex = _compile(lv.get("format"), _field_styles(numbering, lv, level), f"section level {level}", findings)
        if regex is not None:
            regexes[level] = regex
        if level > 1 and _has_appendix(numbering):
            regex = _compile(lv.get("format"), _field_styles(numbering, lv, level, appendix=True),
                             f"appendix section level {level}", findings)
            if regex is not None:
                appendix_regexes[level] = regex
    appendix_level = _mapping(smap.get("appendix"))
    appendix_regex = None
    if appendix_level:
        appendix_styles = _field_styles(numbering, appendix_level, appendix=True)
        appendix_styles["n"] = appendix_styles["n1"]
        appendix_regex = _compile(appendix_level.get("format"), appendix_styles, "appendix heading", findings)
    pairs: list[tuple[int, int]] = []
    appendix_flags: list[bool] = []
    in_appendix = False
    texts = []
    numbered_titles: set[str] = set()
    unnumbered_titles: set[str] = set()
    for h in headings:
        level = int(_HEADING_STYLE.match(h.style).group(1))  # type: ignore[union-attr]
        regex = appendix_regexes.get(level) if in_appendix and level > 1 else regexes.get(level)
        text = h.text.lstrip()
        m = None
        n = None
        if level == 1 and appendix_regex is not None and (in_appendix or regex is None or not regex.match(text)):
            m = appendix_regex.match(text)
            if m is not None:
                n = _number_of(m, appendix_styles, ("n", "n1"))
                in_appendix = n is not None or in_appendix
        if m is None:
            m = regex.match(text) if regex else None
            if m is not None:
                lv = _mapping(smap.get(f"level{level}"))
                n = _number_of(m, _field_styles(numbering, lv, level, appendix=in_appendix), ("n", f"n{level}"))
        if n is None:
            unnumbered_titles.add(_norm(h.text))
            continue
        pairs.append((level, n))
        appendix_flags.append(in_appendix)
        texts.append(_norm(m.group(0)))  # type: ignore[union-attr]
        numbered_titles.add(_norm(text[m.end() :]))  # type: ignore[union-attr]
    extracted["sections"] = {"numbers": [list(p) for p in pairs], "texts": texts}

    # the main chapters and the appendices are two sequences (A restarts at 1)
    for flag in (False, True):
        top = [n for (level, n), app in zip(pairs, appendix_flags, strict=True) if level == 1 and app is flag]
        if len(set(top)) != len(top):
            dup = sorted(n for n, c in Counter(top).items() if c > 1)
            findings.append(_finding("E", "NUM", 10, f"duplicate top-level section numbers {dup} in {top}"))
        if not _is_strictly_increasing(top):
            findings.append(_finding("E", "NUM", 11, f"top-level section numbers are not strictly increasing: {top}"))
    if "sections" in sequences:
        expected_pairs = [
            (e.get("level"), e.get("n")) for e in sequences["sections"] if isinstance(e, Mapping)
        ]
        if expected_pairs != pairs:
            findings.append(
                _finding(
                    "E", "NUM", 12, f"section numbering in DOCX {pairs} differs from the filter report {expected_pairs}"
                )
            )
    for entry in _report_list(ex.report, "unnumbered_headings"):
        title = _norm(str(entry.get("title", "")))
        if title in unnumbered_titles:
            continue
        how = "carries a section number" if title in numbered_titles else "was not found as a heading"
        findings.append(
            _finding("E", "NUM", 13, f"heading {title!r} must be unnumbered (report) but {how} in the DOCX")
        )


def _report_list(report: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = _mapping(report).get(key)
    return [e for e in value if isinstance(e, Mapping)] if isinstance(value, list) else []


def _check_math(
    document: etree._Element, paragraphs: Sequence[_Para], ex: DocxExpectations, result: DocxValidation
) -> None:
    findings, metrics = result.findings, result.metrics
    display = list(document.iter(M_OMATHPARA))
    inside = {o for d in display for o in d.iter(M_OMATH)}  # held elements: lxml proxies are stable while referenced
    metrics["omml_display"] = len(display)
    metrics["omml_inline"] = sum(1 for o in document.iter(M_OMATH) if o not in inside)
    eqarrs = list(document.iter(M_EQARR))
    metrics["eqarr"] = len(eqarrs)

    leaks = 0
    rows: list[str] = []
    for arr in eqarrs:
        for row in arr.findall(M_E):
            text = "".join(t.text or "" for t in row.iter(M_T))
            if "&" in text:
                leaks += text.count("&")
                rows.append(text)
    if leaks:
        findings.append(
            _finding(
                "E", "MATH", 1, f"{leaks} alignment '&' leaked into {len(rows)} equation-array row(s): {rows[:5]}"
            )
        )
    total = sum(t.count("&") for p in paragraphs for t in p.math)
    metrics["math_ampersands"] = total
    math_report = _mapping(_mapping(ex.report).get("math"))
    if "literal_ampersands" in math_report:
        expected = math_report["literal_ampersands"]
        if total != expected:
            findings.append(
                _finding("E", "MATH", 1, f"unexpected & tokens: found {total}, expected {expected}")
            )
    for index, p in enumerate(paragraphs, start=1):
        text = "".join(p.math)
        m = _MATH_ENV.search(text)
        if m:
            findings.append(
                _finding(
                    "E",
                    "MATH",
                    2,
                    f"raw TeX {m.group(0)!r} left in math of paragraph {index} "
                    f"(Pandoc fell back to raw TeX): {_excerpt(text, m.start(), m.end())!r}",
                )
            )


def _drawings(document: etree._Element, doc_rels: Mapping[str, str]) -> list[_Drawing]:
    out: list[_Drawing] = []
    for el in document.iter():
        if el.tag not in (WP_INLINE, WP_ANCHOR):
            continue
        blips = [b for b in el.iter() if b.tag in (A_BLIP, ASVG_BLIP)]
        if not blips:
            continue
        extent = el.find(WP_EXTENT)
        cx = int(extent.get("cx", "0")) if extent is not None else 0
        cy = int(extent.get("cy", "0")) if extent is not None else 0
        targets = [doc_rels[b.get(R_EMBED, "")] for b in blips if b.get(R_EMBED, "") in doc_rels]
        out.append(_Drawing(cx, cy, targets, any(b.tag == ASVG_BLIP for b in blips)))
    return out


def _check_figures(
    document: etree._Element,
    doc_rels: Mapping[str, str],
    hashes: Mapping[str, str],
    ex: DocxExpectations,
    result: DocxValidation,
) -> None:
    findings, metrics = result.findings, result.metrics
    drawings = _drawings(document, doc_rels)
    metrics["drawings"] = len(drawings)
    metrics["svg_drawings"] = sum(1 for d in drawings if d.svg)
    figures = list(ex.figures)
    if not figures:
        return
    if len(drawings) != len(figures):
        findings.append(
            _finding("E", "FIG", 10, f"document has {len(drawings)} drawing(s), expected {len(figures)} figure(s)")
        )
    tol = ex.size_tolerance
    used: set[int] = set()
    matched: dict[int, int] = {}  # figure index -> drawing index
    media_hashes = {h for name, h in hashes.items() if name.startswith("word/media/")}
    for i, fig in enumerate(figures):
        if fig.sha256 is None:
            continue
        if fig.sha256 not in media_hashes:
            findings.append(
                _finding("E", "FIG", 11, f"figure asset missing from the package: figure {fig.id!r} (sha256 {fig.sha256})")
            )
            continue
        for j, d in enumerate(drawings):
            if j not in used and any(hashes.get(t) == fig.sha256 for t in d.targets):
                used.add(j)
                matched[i] = j
                break
    for i, fig in enumerate(figures):
        if i not in matched and i < len(drawings) and i not in used and fig.sha256 is None:
            used.add(i)
            matched[i] = i

    implied: list[tuple[str, float, FigureExpectation]] = []
    metric_rows: list[dict[str, Any]] = []
    for i, fig in enumerate(figures):
        j = matched.get(i)
        if j is None:
            continue
        d = drawings[j]
        cx_in, cy_in = d.cx / EMU_PER_INCH, d.cy / EMU_PER_INCH
        aspect = d.cx / d.cy if d.cy else None
        implied_w = cx_in / fig.width_fraction if fig.width_fraction else None
        metric_rows.append(
            {
                "id": fig.id,
                "cx_in": round(cx_in, 4),
                "cy_in": round(cy_in, 4),
                "aspect": round(aspect, 4) if aspect is not None else None,
                "implied_text_width_in": round(implied_w, 4) if implied_w is not None else None,
            }
        )
        if fig.aspect is not None and (aspect is None or abs(aspect - fig.aspect) > tol * fig.aspect):
            findings.append(
                _finding(
                    "E",
                    "FIG",
                    12,
                    f"figure {fig.id!r} has aspect {aspect if aspect is None else round(aspect, 4)} "
                    f"(cx={d.cx}, cy={d.cy} EMU), expected {round(fig.aspect, 4)}",
                )
            )
        if fig.width_inches is not None and abs(cx_in - fig.width_inches) > tol * fig.width_inches:
            findings.append(
                _finding(
                    "E",
                    "FIG",
                    13,
                    f"figure {fig.id!r} is {cx_in:.3f} in wide, requested {fig.width_inches:.3f} in"
                    + (f" ({fig.requested})" if fig.requested else ""),
                )
            )
        if implied_w is not None:
            implied.append((fig.id, implied_w, fig))
    metrics["figures"] = metric_rows

    if implied:
        median = statistics.median(w for _, w, _ in implied)
        for fid, w, fig in implied:
            if abs(w - median) > tol * median:
                findings.append(
                    _finding(
                        "E",
                        "FIG",
                        13,
                        f"figure {fid!r} implies a text width of {w:.3f} in (width {fig.width_fraction} of text"
                        + (f", requested {fig.requested}" if fig.requested else "")
                        + f"), but the median over all figures is {median:.3f} in",
                    )
                )
            elif ex.text_width_inches is not None and abs(w - ex.text_width_inches) > tol * ex.text_width_inches:
                findings.append(
                    _finding(
                        "E",
                        "FIG",
                        13,
                        f"figure {fid!r} implies a text width of {w:.3f} in, expected {ex.text_width_inches:.3f} in",
                    )
                )
