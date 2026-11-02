"""Layout validation of a final DOCX (publisher-neutral).

Checks what the layout pass promised, read back from the written package: page
geometry, sections and columns, the front-matter/body transition, title/author/
abstract/keyword presence, figure and table fit, equation structure, required
styles, hidden marker paragraphs and direct-formatting density. It never infers
geometry from rendering. Everything that only a rendering can prove (visual
centring, line breaks, pagination) is out of scope here and listed for manual
Word review by the caller.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from latex2word.diagnostics import Severity
from latex2word.errors import BuildError
from latex2word.layout import FrontmatterConfig, ResolvedLayout, resolve_figure_width
from latex2word.metadata.model import DocumentMetadata
from latex2word.ooxml import style_ids
from latex2word.ooxml.drawings import FigureSpec
from latex2word.ooxml.frontmatter import rendered_front_text
from latex2word.ooxml.namespaces import qn, text_of, w
from latex2word.ooxml.package import OpcPackage
from latex2word.ooxml.sections import SectionPlan
from latex2word.ooxml.style_ids import REQUIRED_STYLES, ROLE_STYLES
from latex2word.ooxml.styles import describe_styles, style_ids_in
from latex2word.ooxml.xmlutil import (
    PPR_ORDER,
    RPR_ORDER,
    SECTPR_ORDER,
    STYLE_ORDER,
    TBLPR_ORDER,
    TCPR_ORDER,
    TRPR_ORDER,
    paragraph_style,
)
from latex2word.source_graph import Finding
from latex2word.units import EMU_PER_TWIP, Length

_MARKER = re.compile(r"^[A-Z0-9]{3,}(?:_[A-Z0-9]+)+$")
DIRECT_FORMAT_RATIO_LIMIT = 0.25
_PPR_NEUTRAL = {"pStyle", "rPr", "sectPr", "bookmarkStart"}


@dataclass(frozen=True)
class LayoutExpectations:
    layout: ResolvedLayout
    frontmatter: FrontmatterConfig
    metadata: DocumentMetadata
    expect_title: bool = True
    expect_abstract: bool = False
    expect_keywords: bool = False
    display_equations: int | None = None  # display equations in the preliminary DOCX
    figures: Sequence[FigureSpec] = ()
    numbered_sections: Sequence[dict[str, Any]] = ()


@dataclass
class LayoutValidation:
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(f.severity is Severity.ERROR for f in self.findings)


def _finding(severity: str, number: int, message: str) -> Finding:
    level = {"E": Severity.ERROR, "W": Severity.WARNING, "I": Severity.INFO}[severity]
    return Finding(f"L2W-{severity}-LAYOUT-{number:03d}", level, message)


def _attr_int(element: etree._Element | None, name: str) -> int | None:
    value = element.get(w(name)) if element is not None else None
    return int(value) if value is not None and value.lstrip("-").isdigit() else None


def _section_record(sect: etree._Element) -> dict[str, Any]:
    kind = sect.find(w("type"))
    cols = sect.find(w("cols"))
    size, margin = sect.find(w("pgSz")), sect.find(w("pgMar"))
    return {
        "type": kind.get(w("val")) if kind is not None else "nextPage",
        "columns": _attr_int(cols, "num") or 1,
        "gap_twips": _attr_int(cols, "space"),
        "page_width_twips": _attr_int(size, "w"),
        "page_height_twips": _attr_int(size, "h"),
        "orientation": size.get(w("orient")) if size is not None else None,
        "margins": {side: _attr_int(margin, side) for side in ("top", "bottom", "left", "right", "header", "footer")},
    }


def _check_sections(body: etree._Element, ex: LayoutExpectations, result: LayoutValidation) -> list[etree._Element]:
    layout, findings = ex.layout, result.findings
    sects = [s for s in body.iter(w("sectPr"))]
    records = [_section_record(s) for s in sects]
    result.metrics["section_count"] = len(sects)
    result.metrics["sections"] = records
    # sections that end where the source changes its page numbering (\pagenumbering): next-page breaks
    pagination_breaks = [s for s in sects if s.getparent().tag == w("pPr") and s.find(w("pgNumType")) is not None]
    expected_count = (2 if layout.needs_section_break else 1) + len(pagination_breaks)
    if len(sects) != expected_count:
        findings.append(_finding("E", 72, f"the document has {len(sects)} section(s), expected {expected_count}"))
    page = layout.page
    expected_page = (page.width.twips, page.height.twips)
    expected_margins = {
        "top": page.top.twips, "bottom": page.bottom.twips, "left": page.left.twips, "right": page.right.twips,
        "header": page.header_distance.twips, "footer": page.footer_distance.twips,
    }
    columns = [layout.frontmatter_columns, layout.body_columns] if layout.needs_section_break else [layout.body_columns]
    if pagination_breaks:  # with page-numbering sections only the body (last) section's columns are prescribed
        columns = [None] * (len(records) - 1) + [layout.body_columns]
    for index, record in enumerate(records):
        label = f"section {index + 1}"
        if (record["page_width_twips"], record["page_height_twips"]) != expected_page:
            findings.append(
                _finding("E", 70, f"{label} page size is {record['page_width_twips']}x{record['page_height_twips']} twips, "
                                  f"expected {expected_page[0]}x{expected_page[1]}")
            )
        if record["margins"] != expected_margins:
            findings.append(_finding("E", 71, f"{label} margins are {record['margins']}, expected {expected_margins}"))
        if index < len(columns) and columns[index] is not None:
            if record["columns"] != columns[index]:
                findings.append(_finding("E", 73, f"{label} has {record['columns']} column(s), expected {columns[index]}"))
            if record["columns"] > 1 and record["gap_twips"] != layout.column_gap.twips:
                findings.append(
                    _finding("E", 73, f"{label} column gap is {record['gap_twips']} twips, expected {layout.column_gap.twips}")
                )
    if layout.needs_section_break and len(records) == 2 and records[-1]["type"] != "continuous":
        findings.append(_finding("E", 74, "the front-matter/body transition is not a continuous section break"))
    return sects


def _section_split(body: etree._Element) -> etree._Element | None:
    """The paragraph carrying the front-matter section break (a paragraph-level ``w:sectPr``)."""
    for paragraph in body.findall(w("p")):
        sect = paragraph.find(f"{w('pPr')}/{w('sectPr')}")
        if sect is not None and sect.find(w("pgNumType")) is None:  # page-numbering sections are not the split
            return paragraph
    return None


def _check_pagination(package: OpcPackage, document: etree._Element, body: etree._Element, ex: LayoutExpectations,
                      result: LayoutValidation) -> None:
    """Page-number parts are referenced and carry a PAGE field; TOC fields are marked for update."""
    findings = result.findings
    pagination = ex.layout.pagination
    sects = list(body.iter(w("sectPr")))
    targets = package.relationship_targets("word/document.xml")
    page_parts = 0
    if pagination.part is not None:
        for index, sect in enumerate(sects):
            refs = [r for r in sect.findall(w(f"{pagination.part}Reference")) if r.get(w("type")) == "default"]
            if not refs:
                findings.append(_finding("E", 90, f"section {index + 1} has no default {pagination.part} reference"))
                continue
            target = targets.get(refs[0].get(qn("r:id")))
            if target is None or not package.has(target):
                findings.append(_finding("E", 91, f"section {index + 1} references a missing {pagination.part} part"))
                continue
            instr = " ".join((t.text or "") for t in package.xml(target).iter(w("instrText")))
            if "PAGE" in instr.split():
                page_parts += 1
        if page_parts == 0:
            findings.append(_finding("E", 92, "page numbers are configured but no section shows a PAGE field"))
    result.metrics["pagination"] = {"sections": len(sects), "sections_with_page_field": page_parts}
    toc = [t for t in document.iter(w("instrText")) if (t.text or "").strip().startswith("TOC")]
    if toc:
        settings = package.xml("word/settings.xml") if package.has("word/settings.xml") else None
        update = settings.find(w("updateFields")) if settings is not None else None
        if update is None or update.get(w("val")) not in ("true", "1", "on"):
            findings.append(_finding("E", 93, "TOC fields are present but word/settings.xml does not set w:updateFields=true"))
        begins = [b for b in document.iter(w("fldChar")) if b.get(w("fldCharType")) == "begin"]
        dirty = sum(1 for b in begins if b.get(w("dirty")) in ("true", "1"))
        result.metrics["toc_fields"] = {"count": len(toc), "dirty_begins": dirty}
        if dirty < len(toc):
            findings.append(_finding("E", 94, f"{len(toc) - dirty} TOC field(s) are not marked dirty for update"))


def _check_front(body: etree._Element, document: etree._Element, ex: LayoutExpectations, result: LayoutValidation) -> None:
    findings, metrics = result.findings, result.metrics
    paragraphs = list(document.iter(w("p")))
    styles = [paragraph_style(p) for p in paragraphs]
    title_present = style_ids.TITLE in styles
    metrics["title_present"] = title_present
    if ex.expect_title and not title_present:
        findings.append(_finding("E", 75, "the document has no title paragraph"))
    front = rendered_front_text(document)
    rendered = " ".join(front["authors"])
    metrics["author_blocks"] = len(front["authors"])
    metrics["author_layout"] = front["layout"]
    missing = [a.name for a in ex.metadata.authors if a.name not in rendered]
    if missing:
        findings.append(_finding("E", 76, f"{len(missing)} author name(s) are missing from the front matter: {missing}"))
    for author in ex.metadata.authors:
        if author.email and ex.frontmatter.author_block.show_email and author.email not in rendered:
            findings.append(_finding("E", 76, f"the e-mail address of author {author.name!r} is missing from the front matter"))
    abstract_present = style_ids.ABSTRACT in styles
    keywords_present = style_ids.INDEX_TERMS in styles
    metrics["abstract_present"], metrics["keywords_present"] = abstract_present, keywords_present
    if ex.expect_abstract and not abstract_present:
        findings.append(_finding("E", 77, "the source declares an abstract but the document has none"))
    if ex.expect_keywords and not keywords_present:
        findings.append(_finding("E", 78, "the source declares keywords but the document has none"))
    for block, style, key in (
        (ex.frontmatter.abstract, style_ids.ABSTRACT, "abstract_label"),
        (ex.frontmatter.keywords, style_ids.INDEX_TERMS, "keywords_label"),
    ):
        first = next((p for p in paragraphs if paragraph_style(p) == style), None)
        if first is not None and block.layout == "run-in" and block.label:
            label_ok = " ".join(text_of(first).split()).startswith(block.run_in_text.strip())
            metrics[key] = label_ok
            if not label_ok:
                findings.append(_finding("E", 79, f"the {key.split('_')[0]} paragraph does not start with {block.run_in_text!r}"))
    for p in paragraphs:
        text = " ".join(text_of(p).split())
        # a table cell such as "FILTER_OUT" (a circuit node name) is content, never a leaked layout marker
        if _MARKER.match(text) and not any(a.tag == w("tc") for a in p.iterancestors()):
            findings.append(_finding("E", 83, f"hidden marker paragraph {text!r} found (the layout never writes marker text)"))
    split = _section_split(body)
    if ex.layout.needs_section_break and split is not None:
        following = [c for c in split.itersiblings() if c.tag == w("p")]
        if not following or paragraph_style(following[0]) not in style_ids.HEADING:
            findings.append(_finding("E", 74, "the front-matter section break is not directly followed by a heading"))


def _drawing_records(package: OpcPackage, document: etree._Element) -> list[dict[str, Any]]:
    import hashlib

    targets = package.relationship_targets("word/document.xml")
    records: list[dict[str, Any]] = []
    for node in document.iter(qn("wp:inline"), qn("wp:anchor")):
        blips = [b for b in node.iter(qn("a:blip"), qn("asvg:svgBlip"))]
        if not blips:
            continue
        extent = node.find(qn("wp:extent"))
        shas = {
            hashlib.sha256(package.read(targets[b.get(qn("r:embed"))])).hexdigest()
            for b in blips if targets.get(b.get(qn("r:embed"), "")) and package.has(targets[b.get(qn("r:embed"))])
        }
        records.append({"node": node, "cx": int(extent.get("cx")), "cy": int(extent.get("cy")), "shas": shas,
                        "svg": any(b.tag == qn("asvg:svgBlip") for b in blips)})
    return records


def _check_figures(
    package: OpcPackage, document: etree._Element, body: etree._Element, ex: LayoutExpectations, result: LayoutValidation
) -> None:
    findings = result.findings
    split = _section_split(body) if ex.layout.needs_section_break else None
    plan = SectionPlan(ex.layout, body, split.getnext() if split is not None else None)
    rows: list[dict[str, Any]] = []
    claimed: set[int] = set()
    for record in _drawing_records(package, document):
        geometry = plan.geometry_for(record["node"])
        available = geometry.column_width
        cx = Length(record["cx"])
        spec = next((s for s in ex.figures if s.sha256 and s.sha256 in record["shas"] and id(s) not in claimed), None)
        if spec is not None:
            claimed.add(id(spec))  # one asset may be placed several times: specs are consumed in document order
        row = {"id": spec.id if spec else None, "cx_in": round(cx.in_, 4), "cy_in": round(record["cy"] / 914400, 4),
               "available_in": round(available.in_, 4), "svg": record["svg"]}
        if cx.emu > available.emu + EMU_PER_TWIP and not ex.layout.figures.allow_overflow:
            findings.append(_finding("E", 80, f"figure {row['id'] or '?'} is {cx.in_:.3f} in wide, wider than its {available.in_:.3f} in column"))
        if spec is not None and spec.width:
            resolved = resolve_figure_width(spec.width, geometry, ex.layout)
            if resolved.requested is not None:
                expected = min(resolved.requested.emu, available.emu) if not ex.layout.figures.allow_overflow else resolved.requested.emu
                row["expected_in"] = round(expected / 914400, 4)
                if abs(cx.emu - expected) > EMU_PER_TWIP:
                    findings.append(
                        _finding("E", 81, f"figure {spec.id} is {cx.in_:.4f} in wide, the layout resolves {spec.width.get('original')!r} to {expected / 914400:.4f} in")
                    )
        rows.append(row)
    result.metrics["figures"] = rows


def _table_style(table: etree._Element) -> str:
    node = table.find(f"{w('tblPr')}/{w('tblStyle')}")
    return node.get(w("val"), "") if node is not None else ""


def _check_tables(body: etree._Element, document: etree._Element, ex: LayoutExpectations, result: LayoutValidation) -> None:
    findings = result.findings
    split = _section_split(body) if ex.layout.needs_section_break else None
    plan = SectionPlan(ex.layout, body, split.getnext() if split is not None else None)
    rows: list[dict[str, Any]] = []
    for table in document.iter(w("tbl")):
        if _table_style(table) in style_ids.LAYOUT_TABLE_STYLES:
            continue
        width = table.find(f"{w('tblPr')}/{w('tblW')}")
        grid = sum(_attr_int(c, "w") or 0 for c in table.findall(f"{w('tblGrid')}/{w('gridCol')}"))
        available = plan.geometry_for(table).column_width.twips
        declared = _attr_int(width, "w") if width is not None and width.get(w("type")) == "dxa" else None
        layout_kind = table.find(f"{w('tblPr')}/{w('tblLayout')}")
        row = {"grid_twips": grid, "tblw_twips": declared, "available_twips": available,
               "layout": layout_kind.get(w("type")) if layout_kind is not None else "autofit"}
        if grid > available + 1 or (declared is not None and declared > available + 1):
            findings.append(_finding("E", 82, f"a table is wider than its column (grid {grid}, width {declared}, available {available} twips)"))
        rows.append(row)
    result.metrics["tables"] = rows


def _check_equations(document: etree._Element, ex: LayoutExpectations, result: LayoutValidation) -> None:
    findings = result.findings
    display = list(document.iter(qn("m:oMathPara")))
    numbered = 0
    for paragraph in document.iter(w("p")):
        if paragraph_style(paragraph) != style_ids.EQUATION_NUMBER or not text_of(paragraph).strip():
            continue
        numbered += 1
        row = next((a for a in paragraph.iterancestors(w("tr"))), None)
        scope = row if row is not None else paragraph
        associated = scope.find(f".//{qn('m:oMath')}") is not None
        if row is None and not associated:
            previous = paragraph.getprevious()
            associated = previous is not None and previous.find(f".//{qn('m:oMathPara')}") is not None
        if not associated:
            findings.append(_finding("E", 84, f"equation number {text_of(paragraph).strip()!r} is not associated with an equation"))
    result.metrics["equation_numbers"] = numbered
    result.metrics["display_equations"] = len(display)
    if ex.display_equations is not None and len(display) + sum(
        1 for p in document.iter(w("p")) if paragraph_style(p) == style_ids.EQUATION and p.find(f".//{qn('m:oMathPara')}") is None
        and p.find(f".//{qn('m:oMath')}") is not None
    ) != ex.display_equations:
        findings.append(_finding("E", 85, f"the document has {len(display)} display equation(s), the preliminary DOCX had {ex.display_equations}"))


def _check_styles(package: OpcPackage, ex: LayoutExpectations, result: LayoutValidation) -> None:
    styles = package.xml("word/styles.xml")
    present = style_ids_in(styles)
    missing = [s for s in REQUIRED_STYLES if s not in present]
    if missing:
        result.findings.append(_finding("E", 86, f"required style(s) missing: {', '.join(missing)}"))
    described = describe_styles(styles, sorted(present & {s for ids in ROLE_STYLES.values() for s in ids}))
    result.metrics["styles"] = described
    for role, spec in ex.layout.typography.items():
        primary = ROLE_STYLES[role][0]
        info = described.get(primary)
        if info is None or spec.size_pt is None:
            continue
        if info.get("size_pt") is not None and abs(float(info["size_pt"]) - spec.size_pt) > 0.01:
            result.findings.append(_finding("E", 87, f"style {primary} is {info['size_pt']} pt, the profile asks for {spec.size_pt} pt"))


def _check_direct_formatting(document: etree._Element, result: LayoutValidation) -> None:
    total = direct = 0
    for paragraph in document.iter(w("p")):
        if any(a.tag == w("tc") for a in paragraph.iterancestors()):
            continue
        total += 1
        container = paragraph.find(w("pPr"))
        if container is not None and any(etree.QName(c).localname not in _PPR_NEUTRAL for c in container):
            direct += 1
    ratio = direct / total if total else 0.0
    result.metrics["direct_format"] = {"paragraphs": total, "with_direct_pPr": direct, "ratio": round(ratio, 4)}
    if ratio > DIRECT_FORMAT_RATIO_LIMIT:
        result.findings.append(_finding("W", 88, f"{direct} of {total} paragraphs carry direct paragraph formatting ({ratio:.0%}); styles should carry it"))


_ORDERED_CONTAINERS: dict[str, tuple[str, ...]] = {
    "pPr": PPR_ORDER, "rPr": RPR_ORDER, "tblPr": TBLPR_ORDER, "trPr": TRPR_ORDER, "tcPr": TCPR_ORDER,
    "sectPr": SECTPR_ORDER, "style": STYLE_ORDER,
}


def _check_schema_order(roots: dict[str, etree._Element], result: LayoutValidation) -> None:
    """Word rejects (or silently reorders) property containers whose children break the schema sequence."""
    bad: dict[str, int] = {}
    for part, root in roots.items():
        for container in root.iter(*(w(name) for name in _ORDERED_CONTAINERS)):
            order = _ORDERED_CONTAINERS[etree.QName(container).localname]
            ranks = [order.index(n) for n in (etree.QName(c).localname for c in container if isinstance(c.tag, str)) if n in order]
            if ranks != sorted(ranks):
                bad[part] = bad.get(part, 0) + 1
    result.metrics["schema_order_violations"] = bad
    for part, count in bad.items():
        result.findings.append(_finding("E", 90, f"{count} property container(s) in {part} have children out of schema order"))


def validate_layout(path: Path, expectations: LayoutExpectations) -> LayoutValidation:
    """Validate the layout of the DOCX at ``path`` (the package must already be structurally valid)."""
    result = LayoutValidation()
    try:
        package = OpcPackage.load(path)
        document = package.xml("word/document.xml")
        body = document.find(w("body"))
        if body is None:
            raise BuildError("word/document.xml has no w:body")
        _check_sections(body, expectations, result)
        _check_pagination(package, document, body, expectations, result)
        _check_front(body, document, expectations, result)
        _check_figures(package, document, body, expectations, result)
        _check_tables(body, document, expectations, result)
        _check_equations(document, expectations, result)
        _check_styles(package, expectations, result)
        _check_direct_formatting(document, result)
        _check_schema_order({"word/document.xml": document, "word/styles.xml": package.xml("word/styles.xml")}, result)
    except BuildError as exc:
        result.findings.append(_finding("E", 89, f"the layout of {path.name} could not be read: {exc}"))
    return result
