"""Layout report (machine-readable) and visual review checklist (human) of a finished build.

The report only states what the OOXML proves: geometry, sections, columns, styles, extents. Everything
that only a rendering can show is labelled ``MICROSOFT_WORD_REVIEW_REQUIRED`` and left to a person
opening the DOCX in Microsoft Word; no pixel-perfect equivalence is ever claimed automatically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from latex2word.diagnostics import Severity
from latex2word.layout import ResolvedLayout
from latex2word.metadata.model import DocumentMetadata
from latex2word.ooxml.finalize import FinalizeResult
from latex2word.ooxml.validation import LayoutValidation
from latex2word.source_graph import Finding

LAYOUT_REPORT_SCHEMA = "latex2word.layout_report/1"
REVIEW_LABEL = "MICROSOFT_WORD_REVIEW_REQUIRED"


def build_layout_report(
    profile_id: str,
    profile_hash: str,
    layout: ResolvedLayout,
    metadata: DocumentMetadata,
    finalization: FinalizeResult | None,
    layout_validation: LayoutValidation | None,
) -> dict[str, Any]:
    metrics = finalization.metrics if finalization else {}
    checked = layout_validation.metrics if layout_validation else {}
    return {
        "schema": LAYOUT_REPORT_SCHEMA,
        "profile": {"id": profile_id, "sha256": profile_hash},
        "layout": layout.to_dict(),
        "sections": checked.get("sections", []),
        "metadata_counts": metadata.counts(),
        "frontmatter": metrics.get("frontmatter"),
        "created_styles": metrics.get("created_styles", []),
        "styles": checked.get("styles") or metrics.get("styles"),
        "figures": checked.get("figures", []),
        "figure_outcomes": metrics.get("figures", []),
        "tables": checked.get("tables", []),
        "table_outcomes": metrics.get("tables", []),
        "equation_layout_mode": layout.equations.mode,
        "equations": metrics.get("equations", []),
        "equation_numbers": checked.get("equation_numbers"),
        "display_equations": checked.get("display_equations"),
        "direct_format": checked.get("direct_format"),
    }


def write_layout_report(path: Path, report: dict[str, Any]) -> None:
    path.write_bytes((json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _status(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def render_visual_review(
    *,
    profile_id: str,
    final_name: str,
    preliminary_name: str,
    report: dict[str, Any],
    findings: list[Finding],
) -> str:
    """Markdown checklist: automated structural facts first, then what a person must look at in Word."""
    page = report["layout"]["page"]
    columns = report["layout"]["columns"]
    counts = report["metadata_counts"]
    front = report.get("frontmatter") or {}
    lines = [
        "# Visual review",
        "",
        f"* final DOCX: `{final_name}`",
        f"* preliminary (semantic) DOCX: `{preliminary_name}`",
        f"* profile: `{profile_id}` (sha256 {report['profile']['sha256'][:16]}...)",
        "",
        "## Verified from the OOXML",
        "",
        f"* page: {page['width_twips']} x {page['height_twips']} twips ({page['orientation']}); margins top {page['margin_top_twips']}, "
        f"bottom {page['margin_bottom_twips']}, left {page['margin_left_twips']}, right {page['margin_right_twips']} twips",
        f"* sections: {len(report['sections'])}; "
        + "; ".join(f"#{i + 1} {s['type']} x{s['columns']} gap {s['gap_twips']}" for i, s in enumerate(report["sections"])),
        f"* columns: front matter {columns['frontmatter']}, body {columns['body']}, gap {columns['gap_twips']} twips, "
        f"body column width {columns['body_column_width_in']} in",
        f"* title: {_status(bool(front.get('title_present')))}; authors rendered: {front.get('author_count_rendered', 0)} of "
        f"{counts['authors']} ({front.get('author_layout', 'none')}); abstract: {_status(bool(front.get('abstract_present')))}; "
        f"keywords: {_status(bool(front.get('keywords_present')))}",
        f"* equation layout mode: {report['equation_layout_mode']}; numbered equations: {report.get('equation_numbers')}; "
        f"display equations (native OMML): {report.get('display_equations')}",
        "* figure widths (in): "
        + (", ".join(f"{f.get('id') or '?'} {f['cx_in']} / column {f['available_in']}" for f in report["figures"]) or "none"),
        "* table fit (twips, grid / available): "
        + (", ".join(f"{t['grid_twips']}/{t['available_twips']}" for t in report["tables"]) or "none"),
        "",
        "## Warnings and errors",
        "",
    ]
    shown = [f for f in findings if f.severity is not Severity.INFO]
    lines += [f"* {f}" for f in shown] or ["* none"]
    lines += [
        "",
        f"## {REVIEW_LABEL}",
        "",
        "The automated build cannot render pages. Open the final DOCX in Microsoft Word and check:",
        "",
        f"1. {REVIEW_LABEL}: title and front matter look right (font, centring, spacing).",
        f"2. {REVIEW_LABEL}: author block - names, affiliations, e-mail addresses, accents, wrapping, no visible borders.",
        f"3. {REVIEW_LABEL}: abstract and keyword labels and punctuation.",
        f"4. {REVIEW_LABEL}: the first column transition (one column to the body columns) is clean, with no stray blank line.",
        f"5. {REVIEW_LABEL}: every numbered equation is visually centred with its number at the right margin, including the multi-line one.",
        f"6. {REVIEW_LABEL}: figures fit their column; captions sit on the intended side; vector figures are crisp.",
        f"7. {REVIEW_LABEL}: tables fit the column, rules and header look right, captions are attached.",
        f"8. {REVIEW_LABEL}: headings, acknowledgment (unnumbered) and the references list (hanging indent).",
        f"9. {REVIEW_LABEL}: pagination and float placement (never compared with any LaTeX PDF).",
        "",
    ]
    return "\n".join(lines)


def write_visual_review(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))
