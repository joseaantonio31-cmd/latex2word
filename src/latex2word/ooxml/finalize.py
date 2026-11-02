"""The single OOXML finalisation pass: preliminary DOCX -> publisher-layout DOCX.

The preliminary package is loaded once, every layout transform runs on the in-memory
trees, the result is written to a temporary sibling, validated there, and only then
atomically moved onto the destination. The preliminary file is never modified, and a
failed finalisation leaves a previous destination file byte-for-byte intact.

Transform order (each is generic and driven by the resolved layout/profile data):
styles -> body start -> front matter -> bibliography -> caption placement -> figure extents -> table layout ->
equation layout -> section properties -> core properties.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from latex2word.diagnostics import Severity
from latex2word.errors import BuildError
from latex2word.layout import FrontmatterConfig, ResolvedLayout
from latex2word.logging_utils import log_event
from latex2word.metadata.model import DocumentMetadata
from latex2word.ooxml.bibliography import apply_bibliography_layout
from latex2word.ooxml.drawings import FigureSpec, apply_caption_placement, apply_figure_extents
from latex2word.ooxml.equations import apply_equation_layout
from latex2word.ooxml.fields import field_instructions, keep_line_breaks_unstretched, request_field_update
from latex2word.ooxml.frontmatter import apply_core_properties, apply_frontmatter
from latex2word.ooxml.namespaces import w
from latex2word.ooxml.package import OpcPackage
from latex2word.ooxml.pagination import apply_pagination
from latex2word.ooxml.headings import apply_unlisted_headings, unindent_after_rules
from latex2word.ooxml.notes import apply_caption_notes
from latex2word.ooxml.sections import SectionPlan, apply_section_properties, find_body_start
from latex2word.ooxml.styles import apply_size_styles, describe_styles, ensure_required_styles
from latex2word.ooxml.style_ids import ROLE_STYLES
from latex2word.ooxml.tables import apply_table_layout, restyle_image_grids, restyle_uncaptioned_tables
from latex2word.source_graph import Finding

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FinalizeContext:
    layout: ResolvedLayout
    frontmatter: FrontmatterConfig
    metadata: DocumentMetadata
    figures: Sequence[FigureSpec] = ()
    numbered_sections: Sequence[dict[str, Any]] = ()  # filter report: numbered headings in order (level/text/title)


@dataclass
class FinalizeResult:
    ok: bool = False
    output: Path | None = None
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]


def _error(code: str, message: str) -> Finding:
    return Finding(code, Severity.ERROR, message)


def finalize_docx(
    preliminary: Path,
    destination: Path,
    context: FinalizeContext,
    *,
    validate: Callable[[Path], list[Finding]] | None = None,
) -> FinalizeResult:
    """Finalise ``preliminary`` into ``destination`` (see the module docstring); never raises for document problems."""
    result = FinalizeResult()
    layout = context.layout
    try:
        package = OpcPackage.load(preliminary)
        document = package.xml("word/document.xml")
        body = document.find(w("body"))
        if body is None:
            raise BuildError("word/document.xml has no w:body")
        styles = package.xml("word/styles.xml")
        created = ensure_required_styles(styles, layout)

        body_start = None
        if layout.needs_section_break:
            body_start = find_body_start(body, layout.body_start, context.numbered_sections)
            if body_start is None:
                result.findings.append(
                    _error("L2W-E-LAYOUT-060",
                           f"the body start ({layout.body_start}) was not found, but the layout needs a "
                           f"{layout.frontmatter_columns}->{layout.body_columns} column section break")
                )
                return result
        plan = SectionPlan(layout, body, body_start)

        front, found = apply_frontmatter(document, context.metadata, layout, context.frontmatter)
        result.findings.extend(found)
        bibliography = apply_bibliography_layout(document)
        result.findings.extend(apply_caption_placement(document, layout))
        figures, found = apply_figure_extents(package, document, plan, context.figures)
        result.findings.extend(found)
        image_grids = restyle_image_grids(document)
        uncaptioned_tables = restyle_uncaptioned_tables(document, layout)
        tables, found = apply_table_layout(document, plan)
        result.findings.extend(found)
        equations, found = apply_equation_layout(document, plan)
        result.findings.extend(found)
        sections = apply_section_properties(body, layout, body_start)
        pagination = apply_pagination(package, document, layout.pagination)
        unlisted = apply_unlisted_headings(document, styles)
        unindent_after_rules(document)
        size_styles = apply_size_styles(styles, document, dict(layout.font_sizes))
        caption_notes = apply_caption_notes(document, styles, layout)
        apply_core_properties(package, context.metadata)
        keep_line_breaks_unstretched(package)
        toc_fields = [i for i in field_instructions(document) if i.split()[:1] == ["TOC"]]
        if toc_fields:
            request_field_update(package)  # Word fills the tables of contents when it opens the file
        used = sorted({style for role_styles in ROLE_STYLES.values() for style in role_styles})
        result.metrics = {
            "sections": sections,
            "created_styles": created,
            "frontmatter": front.to_dict(),
            "bibliography": bibliography.to_dict(),
            "figures": [f.to_dict() for f in figures],
            "tables": [t.to_dict() for t in tables],
            "equations": [e.to_dict() for e in equations],
            "toc_fields": toc_fields,
            "pagination": pagination.to_dict(),
            "unlisted_headings": unlisted,
            "size_styles": size_styles,
            "uncaptioned_tables": uncaptioned_tables,
            "image_grids": image_grids,
            "caption_notes": caption_notes,
            "styles": describe_styles(styles, used),
        }
        data = package.to_bytes()
    except (BuildError, ValueError, KeyError) as exc:
        result.findings.append(_error("L2W-E-LAYOUT-061", f"layout finalisation failed: {exc}"))
        return result

    temporary = destination.with_name(f".{destination.name}.tmp")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.write_bytes(data)
        if validate is not None:
            result.findings.extend(validate(temporary))
        if result.errors:
            return result
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    result.ok = True
    result.output = destination
    log_event(_log, logging.INFO, "finalised docx", sections=sections, figures=len(figures), equations=len(equations))
    return result
