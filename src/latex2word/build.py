"""``latex2word build``: LaTeX project -> semantic DOCX -> publisher-layout DOCX.

Pipeline (all side effects stay inside the latex2word-owned workspace; the
requested output is replaced atomically, and only after every check passed)::

    config + profile -> inspect -> workspace + staging -> render figures (M2)
    -> metadata adapter (front matter) + rewrite the *staged* copy (figures, widths, bibliography, metadata)
    -> unknown-construct probe -> profile-generated reference.docx
    -> Pandoc + latex2word Lua filters (+ citeproc) -> structural DOCX validation (preliminary.docx)
    -> one OOXML layout pass -> structural + layout validation (final.docx)
    -> build_manifest.json, layout_report.json, visual_review.md -> atomic output

Workspace layout added by this stage::

    <ws>/build/transformations.json   every staged rewrite (source, location, original, replacement, reason)
    <ws>/build/filter_config.json     resolved profile policy handed to the Lua filters
    <ws>/build/filter_report.json     label registry, numbering sequences, references (from the filters)
    <ws>/build/reference/reference.docx  profile-generated (or external-template copy) Pandoc reference document
    <ws>/build/preliminary.docx       Pandoc output (semantic M3 result), validated
    <ws>/build/final.docx             the layout pass output: the deliverable
    <ws>/build/layout_report.json     page/section/column/style/front-matter/figure/table/equation metrics
    <ws>/build/visual_review.md       human review checklist (MICROSOFT_WORD_REVIEW_REQUIRED items)
    <ws>/build/pandoc_run.json        argv, cwd, exit code, elapsed time, warnings, stderr
    <ws>/build_manifest.json          reproducibility record of the whole build
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

from latex2word import __version__
from latex2word.bibliography import BibliographyPlan, plan_bibliography
from latex2word.config import ProjectConfig
from latex2word.diagnostics import Severity
from latex2word.docx_validate import DocxExpectations, DocxValidation, FigureExpectation, validate_docx
from latex2word.errors import BuildError
from latex2word.figure_pipeline import FiguresRun, run_figures
from latex2word.figure_registry import sha256_file
from latex2word.figure_rewrite import FigurePlan, copy_assets, plan_figure_rewrites, remaining_tikz
from latex2word.labels import check_labels, scan_labels
from latex2word.logging_utils import log_event
from latex2word.metadata.adapters import MetadataExtraction, get_adapter
from latex2word.metadata.model import DocumentMetadata
from latex2word.ooxml.drawings import FigureSpec
from latex2word.ooxml.style_ids import IMAGE_CAPTION, INDEX_TERMS, STYLE_NAMES, TABLE_CAPTION
from latex2word.ooxml.finalize import FinalizeContext, FinalizeResult, finalize_docx
from latex2word.ooxml.validation import LayoutExpectations, LayoutValidation, validate_layout
from latex2word.pandoc_adapter import PandocOptions, PandocRun, PandocTool, locate_pandoc, run_pandoc
from latex2word.pandoc_compat import PandocCompat, assess_pandoc, compat_findings
from latex2word.process import run_command
from latex2word.profiles import Profile, load_profile
from latex2word.reference_doc import ReferenceDoc, build_reference_docx
from latex2word.review import build_layout_report, render_visual_review, write_layout_report, write_visual_review
from latex2word.columntypes import plan_column_type_rewrites, plan_longtable_rewrites, plan_multicolumn_rewrites
from latex2word.structure_rewrite import (
    contents_titles, plan_note_rewrites, plan_structure_rewrites, plan_typesetting_rewrites,
)
from latex2word.siunitx import babel_language, plan_siunitx_rewrites
from latex2word.rewrite import apply_edits, drop_contained, manifest as transformation_manifest
from latex2word.source_graph import Finding, SourceLocation, build_source_graph
from latex2word.staging import staging_dir
from latex2word.texprobe import probe
from latex2word.toolrun import Runner, Which
from latex2word.workspace import Workspace, is_link_like, verify_owned

_log = logging.getLogger(__name__)

BUILD_MANIFEST_SCHEMA = "latex2word.build_manifest/1"
BUILD_DIRNAME = "build"
FILTER_FILES = ("l2w_semantic.lua", "l2w_math.lua", "l2w_util.lua")
ENTRY_FILTER = "l2w_semantic.lua"


@dataclass
class BuildResult:
    ok: bool = False
    output: Path | None = None
    workspace: Path | None = None
    manifest_path: Path | None = None
    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    validation: DocxValidation | None = None
    profile_id: str | None = None
    preliminary: Path | None = None
    final: Path | None = None
    layout_report: Path | None = None
    visual_review: Path | None = None

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "latex2word.build/1",
            "status": "OK" if self.ok else "FAILED",
            "profile": self.profile_id,
            "output": self.output.as_posix() if self.output else None,
            "workspace": self.workspace.as_posix() if self.workspace else None,
            "manifest": self.manifest_path.as_posix() if self.manifest_path else None,
            "preliminary": self.preliminary.as_posix() if self.preliminary else None,
            "final": self.final.as_posix() if self.final else None,
            "layout_report": self.layout_report.as_posix() if self.layout_report else None,
            "visual_review": self.visual_review.as_posix() if self.visual_review else None,
            "summary": self.summary,
            "diagnostics": [f.to_dict() for f in self.findings if f.severity is not Severity.INFO],
        }


# ---------------------------------------------------------------------------- helpers


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, data: Any) -> None:
    path.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _as_list(value: Any) -> list[Any]:
    """Lua encodes an empty table as ``{}``; every list-valued report field may arrive that way."""
    if isinstance(value, dict):
        return [] if not value else list(value.values())
    return list(value) if isinstance(value, list) else []


def _normalise_report(report: dict[str, Any]) -> dict[str, Any]:
    for key in ("labels", "refs", "citations", "raw_tex", "unnumbered_headings", "diagnostics"):
        report[key] = _as_list(report.get(key))
    sequences = report.get("sequences") or {}
    report["sequences"] = {k: _as_list(sequences.get(k)) for k in ("figures", "tables", "equations", "sections")}
    bibliography = report.get("bibliography") or {}
    bibliography["manual_entries"] = _as_list(bibliography.get("manual_entries"))
    report["bibliography"] = bibliography
    return report


def _filter_dir() -> Path:
    return Path(str(resources.files("latex2word").joinpath("filters")))


def _reset_build_dir(workspace: Workspace) -> Path:
    verify_owned(workspace.path)
    target = workspace.path / BUILD_DIRNAME
    if is_link_like(target):
        raise BuildError(f"build directory is a symlink or junction: {target}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    return target


def _atomic_copy(source: Path, destination: Path) -> None:
    """Copy ``source`` over ``destination`` so that readers see the old file or the new one, never a partial one."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(handle)
    temp = Path(temp_name)
    try:
        shutil.copyfile(source, temp)
        os.replace(temp, destination)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _severity(name: str) -> Severity:
    return {"error": Severity.ERROR, "warning": Severity.WARNING}.get(name, Severity.INFO)


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Drop exact repeats and filter findings that restate a located source-scan finding."""
    located = {
        (f.code, f.message.split('"')[1]) for f in findings
        if f.location is not None and f.code in ("L2W-E-LABEL-001", "L2W-E-REF-001") and '"' in f.message
    }
    seen: set[tuple[str, str, str]] = set()
    kept: list[Finding] = []
    for finding in findings:
        if finding.location is None and finding.code in ("L2W-E-LABEL-001", "L2W-E-REF-001") and '"' in finding.message:
            if (finding.code, finding.message.split('"')[1]) in located:
                continue
        key = (finding.code, finding.message, str(finding.location))
        if key in seen:
            continue
        seen.add(key)
        kept.append(finding)
    return kept


def _classify_raw(raw: dict[str, Any]) -> Finding:
    text = " ".join(str(raw.get("text", "")).split())
    excerpt = text if len(text) <= 80 else text[:77] + "..."
    lowered = text.lower()
    if "cite" in lowered:
        return Finding("L2W-E-CITE-002", Severity.ERROR, f"raw citation command left in the document: {excerpt}")
    if "ref" in lowered.split("{")[0]:
        return Finding("L2W-E-REF-006", Severity.ERROR, f"raw reference command left in the document: {excerpt}")
    return Finding(
        "L2W-W-TEX-005", Severity.WARNING,
        f"raw TeX survived conversion and is dropped by the DOCX writer: {excerpt}",
    )


def _figure_expectations(plan: FigurePlan) -> list[FigureExpectation]:
    expectations: list[FigureExpectation] = []
    for info in plan.figures:
        fraction = inches = None
        requested = None
        if info.width is not None:
            requested = info.width.original
            if info.width.kind == "relative":
                fraction = info.width.fraction
            elif info.width.kind == "absolute":
                inches = info.width.inches
        expectations.append(
            FigureExpectation(info.id, info.asset_sha256, info.aspect, fraction, inches, requested)
        )
    return expectations


def _counts(report: dict[str, Any], plan: BibliographyPlan, figure_plan: FigurePlan) -> dict[str, Any]:
    sequences = report["sequences"]
    return {
        "sections_numbered": len(sequences["sections"]),
        "sections_unnumbered": len(report["unnumbered_headings"]),
        "figures": len(sequences["figures"]),
        "active_figure_assets": len(figure_plan.figures),
        "tables": len(sequences["tables"]),
        "equations": len(sequences["equations"]),
        "labels": len(report["labels"]),
        "references": len(report["refs"]),
        "citation_keys": len(report["citations"]),
        "bibliography_entries_expected": plan.expected_entries,
        "math_display": report.get("math", {}).get("display", 0),
        "math_inline": report.get("math", {}).get("inline", 0),
        "math_alignment_environments_normalised": report.get("math", {}).get("normalized_alignment", 0),
        "math_literal_ampersands": report.get("math", {}).get("literal_ampersands", 0),
    }


_FIELD_PLACEHOLDER = {
    "spanish": "Actualice este campo (F9) para generar la lista.",
    "english": "Update this field (F9) to build the list.",
}


def _contents_config(graph: Any) -> dict[str, Any]:
    """Titles, caption styles and placeholder text of the TOC fields the filter writes."""
    main = graph.files.get(graph.main)
    language = None
    if main is not None:
        document = re.search(r"\\begin\s*\{document\}", main.masked)
        limit = document.start() if document else len(main.masked)
        language = babel_language(main.masked[:limit], main.text[:limit])
    return {
        "titles": contents_titles(graph, language),
        "lof_style": STYLE_NAMES[IMAGE_CAPTION],
        "lot_style": STYLE_NAMES[TABLE_CAPTION],
        "placeholder": _FIELD_PLACEHOLDER.get(language or "english", _FIELD_PLACEHOLDER["english"]),
    }


def _numbering_sequences(report: dict[str, Any]) -> dict[str, Any]:
    sequences = report["sequences"]
    return {
        "sections": [{"level": s["level"], "text": s["text"], "title": s.get("title")} for s in sequences["sections"]],
        "unnumbered_headings": [h.get("title") for h in report["unnumbered_headings"]],
        "figures": [s["text"] for s in sequences["figures"]],
        "tables": [s["text"] for s in sequences["tables"]],
        "equations": [s["text"] for s in sequences["equations"]],
    }


# ---------------------------------------------------------------------------- build


def run_build(
    config: ProjectConfig,
    *,
    output: Path | None = None,
    keep_workspace: bool = False,
    which: Which = shutil.which,
    runner: Runner = run_command,
    progress: Callable[[str], None] | None = None,
) -> BuildResult:
    """Run the whole pipeline for ``config``.

    Raises :class:`~latex2word.errors.ConfigError` (profile problems) or
    :class:`BuildError` (Pandoc unusable) for problems that are not about the
    document; document problems are returned as findings with ``ok=False``.
    """
    note = progress or (lambda _message: None)
    result = BuildResult()
    profile: Profile = load_profile(config.profile, config.profile_dirs)
    result.profile_id = profile.id
    tool: PandocTool = locate_pandoc(which=which, runner=runner)
    note(f"profile {profile.id} ({' > '.join(profile.chain)}); pandoc {tool.version}")
    # The same classification `doctor` reports. Without it a Pandoc whose reader AST differs from the pinned facts
    # was used silently (Pandoc 3.1.3 drops numbered-equation wrappers and \Cref without any diagnostic).
    assessment = assess_pandoc(tool.version, path=tool.path, runner=runner, timeout_s=config.tools.timeout_s, probe=True)
    if assessment.compat is PandocCompat.UNSUPPORTED:
        # a tool problem, not a document one: same contract as an unusable Pandoc in locate_pandoc (exit code 2)
        finding = compat_findings(assessment)[0]
        raise BuildError(f"{finding.message} [{finding.code}]")
    result.findings.extend(f for f in compat_findings(assessment) if f.severity is not Severity.INFO)

    # 1-3: inspect, workspace, staging, figures (M2)
    note("inspecting, staging and rendering figures")
    figures_run: FiguresRun = run_figures(config, which=which, runner=runner)
    result.findings.extend(f for f in figures_run.inspection.findings if f.severity is not Severity.INFO)
    for problem in figures_run.problems:
        result.findings.append(Finding("L2W-E-BUILD-001", Severity.ERROR, problem))
    if figures_run.workspace is None:
        return _finish(result, config, profile, tool, None, None, None, None, None, None, None, output, keep_workspace)
    workspace = Workspace.open(figures_run.workspace, config.project.root)
    result.workspace = workspace.path
    for outcome in figures_run.failed:
        result.findings.append(
            Finding("L2W-E-FIG-001", Severity.ERROR, f"figure {outcome.record.id} could not be rendered: {outcome.error}",
                    SourceLocation(outcome.record.source_file, outcome.record.line))
        )
    if figures_run.problems or figures_run.failed or figures_run.inspection.has_errors:
        return _finish(result, config, profile, tool, workspace, figures_run, None, None, None, None, None, output,
                       keep_workspace)

    inspection = figures_run.inspection
    graph = inspection.graph
    staging = staging_dir(workspace)
    build_dir = _reset_build_dir(workspace)
    main_rel = inspection.main
    main_dir = PurePosixPath(main_rel).parent
    cwd = staging if str(main_dir) in ("", ".") else staging / Path(*main_dir.parts)

    # 4: plans
    note("planning staged-copy rewrites")
    figure_plan = plan_figure_rewrites(figures_run, main_rel)
    bib_plan = plan_bibliography(graph, config.bibliography, profile.bibliography, profile.csl, config.project.root)
    column_plan = plan_column_type_rewrites(graph)
    siunitx_plan = plan_siunitx_rewrites(graph)
    structure_plan = plan_structure_rewrites(
        graph, place_bibliography=bib_plan.mode == "citeproc", follow_pagination=profile.layout.pagination.follow_source
    )
    default_text = profile.layout.typography.get("default")
    typeset_edits, typeset_findings = plan_typesetting_rewrites(
        graph, (default_text.size_pt if default_text is not None and default_text.size_pt else 10.0)
    )
    result.findings.extend(typeset_findings)
    note_edits, note_spans = plan_note_rewrites(
        graph, (default_text.size_pt if default_text is not None and default_text.size_pt else 10.0)
    )
    # a note block keeps its own declarations: no other typesetting marker inside it
    typeset_edits = [
        e for e in typeset_edits if not any(f == e.file and a <= e.start < b for f, a, b in note_spans)
    ]
    if structure_plan.bibliography_title:
        # the document's own \printbibliography[title=...] wins over the profile default
        bib_plan.heading = structure_plan.bibliography_title
    label_scan = scan_labels(graph)
    result.findings.extend(figure_plan.findings)
    result.findings.extend(column_plan.findings)
    result.findings.extend(siunitx_plan.findings)
    result.findings.extend(structure_plan.findings)
    result.findings.extend(bib_plan.findings)
    result.findings.extend(check_labels(label_scan))
    note(f"extracting front matter ({profile.frontmatter.metadata_adapter})")
    extraction = get_adapter(profile.frontmatter.metadata_adapter).extract(graph, profile.frontmatter)
    result.findings.extend(extraction.findings)

    # 5: rewrite the staged copy (never the source)
    copy_assets(figure_plan, workspace.path, staging)
    # a table inside a drawing that is replaced by its SVG needs no column expansion: the outer edit wins
    edits = drop_contained([*figure_plan.edits, *column_plan.edits, *structure_plan.edits, *typeset_edits, *note_edits, *siunitx_plan.edits,
                            *plan_longtable_rewrites(graph), *plan_multicolumn_rewrites(graph),
                            *bib_plan.edits, *extraction.edits])
    records, changes = apply_edits(graph, staging, edits)
    _write_json(build_dir / "transformations.json", transformation_manifest(records, changes))
    rewritten = {
        rel: (staging / Path(*PurePosixPath(rel).parts)).read_text(encoding="utf-8-sig", errors="replace")
        for rel in graph.tex_order
    }
    for rel, line in remaining_tikz(rewritten):
        result.findings.append(
            Finding("L2W-E-FIG-005", Severity.ERROR, "a TikZ/circuitikz environment was not replaced by a figure asset",
                    SourceLocation(rel, line))
        )

    # 6: unknown-construct probe
    note("probing for constructs Pandoc would drop")
    tex_policy = profile.data["tex"]
    staged_graph = build_source_graph(staging, staging / Path(*PurePosixPath(main_rel).parts))
    probe_result = probe(
        staged_graph, tool, build_dir / "probe",
        accepted_commands=tex_policy["accepted_commands"], accepted_environments=tex_policy["accepted_environments"],
        consumed_commands=extraction.consumed_commands, consumed_environments=extraction.consumed_environments,
        allowed_commands=config.tex.allow_commands, allowed_environments=config.tex.allow_environments,
        timeout_s=config.tools.timeout_s, runner=runner,
    )
    result.findings.extend(probe_result.findings)

    # 7: reference document, then Pandoc + filters
    note("generating the reference document")
    try:
        reference = build_reference_docx(profile, tool, build_dir / "reference", timeout_s=config.tools.timeout_s)
    except BuildError as exc:
        result.findings.append(Finding("L2W-E-REFDOC-001", Severity.ERROR, str(exc)))
        return _finish(result, config, profile, tool, workspace, figures_run, figure_plan, bib_plan, None, None, None,
                       output, keep_workspace, records=records, changes=changes, extraction=extraction)
    note("running Pandoc")
    filter_dir = _filter_dir()
    report_path = build_dir / "filter_report.json"
    filter_config = {
        "schema": "latex2word.filter_config/1",
        "report_path": report_path.as_posix(),
        "numbering": profile.numbering,
        "bibliography": {
            "mode": bib_plan.mode,
            "heading": bib_plan.heading,
            "manual": profile.bibliography["manual"],
        },
        "source_labels": label_scan.names,
        "frontmatter": {
            "keyword_environments": list(profile.frontmatter.keywords.environments),
            "keywords_style": STYLE_NAMES[INDEX_TERMS],
        },
        "contents": _contents_config(graph),
        # tocloft custom lists: extension -> Word TOC identifier letter and title
        "custom_lists": {c.ext: {"letter": c.letter, "title": c.title} for c in structure_plan.custom_lists},
        "page_breaks": {
            # a \clearpage right before a level-1 heading is redundant when that style starts a new page
            "before_level1": bool(
                ((profile.data.get("layout") or {}).get("typography") or {}).get("heading1", {}).get("page_break_before")
            ),
        },
        # manual TOC entries (ddcontentsline{toc}{<unit>}{...}) -> TC level; lists use their own TOC level
        "toc_levels": structure_plan.toc_levels,
        "list_level": profile.layout.contents_list_level,
        "bibliography_in_toc": structure_plan.bibliography_in_toc,
    }
    config_path = build_dir / "filter_config.json"
    _write_json(config_path, filter_config)
    metadata = {"l2w-config": config_path.as_posix()}
    if bib_plan.mode == "citeproc" and bib_plan.heading:
        metadata["reference-section-title"] = bib_plan.heading
    preliminary = build_dir / "preliminary.docx"
    options = PandocOptions(
        inputs=[PurePosixPath(main_rel).name],
        output=preliminary,
        lua_filters=[filter_dir / ENTRY_FILTER],
        metadata=metadata,
        citeproc=bib_plan.mode == "citeproc",
        bibliography=[staging / Path(*PurePosixPath(rel).parts) for rel in bib_plan.files] if bib_plan.mode == "citeproc" else (),
        csl=bib_plan.csl if bib_plan.mode == "citeproc" else None,
        reference_doc=reference.path,
        resource_paths=[cwd],
        extra_args=profile.data["pandoc"]["extra_args"],
    )
    pandoc_run: PandocRun = run_pandoc(tool, options, cwd=cwd, timeout_s=config.tools.timeout_s, runner=runner)
    _write_json(build_dir / "pandoc_run.json", pandoc_run.to_dict())
    missing_keys = {key for key in bib_plan.cited_keys if key not in bib_plan.defined}
    for warning in pandoc_run.warnings:
        if warning.code == "L2W-E-CITE-001" and any(re.search(rf"\b{re.escape(k)}\b", warning.text) for k in missing_keys):
            continue  # already reported, with its source location, before conversion
        result.findings.append(warning.to_finding())
    failure = pandoc_run.failure_finding()
    if failure is not None:
        result.findings.append(failure)
    if not pandoc_run.ok or not preliminary.is_file() or not report_path.is_file():
        if pandoc_run.ok:
            result.findings.append(Finding("L2W-E-BUILD-002", Severity.ERROR, "Pandoc finished but produced no DOCX or filter report"))
        return _finish(result, config, profile, tool, workspace, figures_run, figure_plan, bib_plan, None, pandoc_run,
                       None, output, keep_workspace, records=records, changes=changes)

    report = _normalise_report(json.loads(report_path.read_text(encoding="utf-8")))
    for entry in report["diagnostics"]:
        message = entry.get("message", "")
        detail = entry.get("detail")
        result.findings.append(
            Finding(entry.get("code", "L2W-E-FILTER-000"), _severity(entry.get("severity", "error")),
                    f"{message}" + (f" [{detail}]" if detail else ""))
        )
    for raw in report["raw_tex"]:
        result.findings.append(_classify_raw(raw))
    for cite in report["citations"]:
        if bib_plan.mode != "none" and cite["id"] not in bib_plan.defined and cite["id"] != "*":
            if not any(f.code == "L2W-E-CITE-001" and repr(cite["id"]) in f.message for f in result.findings):
                result.findings.append(
                    Finding("L2W-E-CITE-001", Severity.ERROR, f"citation key {cite['id']!r} is not defined in the bibliography")
                )
    result.findings.extend(_lost_references(scan_labels(staged_graph), report, label_scan))
    if _source_has_author(graph) and not extraction.metadata.authors and not any(
        f.code.startswith("L2W-E-META") for f in extraction.findings
    ):
        result.findings.append(
            Finding("L2W-E-META-001", Severity.ERROR,
                    "the source declares \\author but the metadata adapter extracted no author; its content would be lost")
        )
    meta = report["meta"]
    metadata = extraction.metadata.with_document_text(
        title=meta.get("title_text") or None,
        abstract=meta.get("abstract_text") or None,
        keywords=_split_keywords(meta.get("keywords_text")),
    )

    # 8: structural validation
    note("validating the preliminary DOCX")
    expectations = DocxExpectations(
        numbering=profile.numbering,
        report=report,
        figures=_figure_expectations(figure_plan),
        bibliography_entries=bib_plan.expected_entries,
        citations_expected=bool(bib_plan.cited) and bib_plan.mode != "none",
    )
    validation = validate_docx(preliminary, expectations)
    result.findings.extend(validation.findings)
    result.validation = validation
    result.preliminary = preliminary

    # 9: the single layout pass, validated before it replaces anything
    note("applying the profile layout")
    final = build_dir / "final.docx"
    specs = [
        FigureSpec(i.id, i.asset_sha256, i.width.to_dict() if i.width else None, i.aspect) for i in figure_plan.figures
    ]
    numbered = [dict(s) for s in report["sequences"]["sections"]]
    final_expectations = DocxExpectations(
        numbering=profile.numbering,
        report=report,
        figures=[
            FigureExpectation(i.id, i.asset_sha256, i.aspect, None, None, i.width.original if i.width else None)
            for i in figure_plan.figures
        ],
        bibliography_entries=bib_plan.expected_entries,
        citations_expected=bool(bib_plan.cited) and bib_plan.mode != "none",
    )
    layout_expectations = LayoutExpectations(
        layout=profile.layout, frontmatter=profile.frontmatter, metadata=metadata,
        expect_title=bool(metadata.title), expect_abstract=bool(meta.get("abstract")),
        expect_keywords=bool(metadata.keywords) or bool(_as_list(meta.get("keywords_text"))),
        display_equations=validation.metrics.get("omml_display"), figures=specs, numbered_sections=numbered,
    )
    checks: dict[str, Any] = {}

    def validate_final(path: Path) -> list[Finding]:
        semantic = validate_docx(path, final_expectations)
        layout_check = validate_layout(path, layout_expectations)
        checks["semantic"], checks["layout"] = semantic, layout_check
        return [*semantic.findings, *layout_check.findings]

    finalization = finalize_docx(
        preliminary, final, FinalizeContext(profile.layout, profile.frontmatter, metadata, specs, numbered),
        validate=validate_final,
    )
    result.findings.extend(finalization.findings)
    if finalization.ok:
        result.final = final
        result.validation = checks["semantic"]
    return _finish(result, config, profile, tool, workspace, figures_run, figure_plan, bib_plan, report, pandoc_run,
                   final if finalization.ok else None, output, keep_workspace, records=records, changes=changes,
                   validation=validation, reference=reference, extraction=extraction, metadata=metadata,
                   finalization=finalization, final_validation=checks.get("semantic"),
                   layout_validation=checks.get("layout"), preliminary_path=preliminary)


# Reference commands that Pandoc's LaTeX reader turns into links (\pageref/\nameref are reported by the probe).
_LINKED_REFERENCE_COMMANDS = frozenset({"ref", "eqref", "autoref", "cref", "Cref", "vref"})


def _lost_references(scan: Any, report: dict[str, Any], source_scan: Any = None) -> list[Finding]:
    """Labels referenced in the staged source that never reached the filter as a reference.

    Independent of the Pandoc version: a reader that drops a reference command (Pandoc 3.1.3 drops ``\\Cref``
    together with its argument, without a warning) would otherwise remove the text silently.
    """
    seen = {str(ref.get("label")) for ref in report["refs"]}
    # staged line numbers can shift after rewrites: report the original source location when there is one
    source_location = {
        (r.command, r.label): r.location for r in reversed(source_scan.references if source_scan else ())
    }
    findings: list[Finding] = []
    reported: set[str] = set()
    for reference in scan.references:
        if reference.command not in _LINKED_REFERENCE_COMMANDS or reference.label in seen or reference.label in reported:
            continue
        if reference.in_definition:  # a macro body is only text until used (and may never be used)
            continue
        reported.add(reference.label)
        findings.append(
            Finding(
                "L2W-E-REF-012", Severity.ERROR,
                f'\\{reference.command}{{{reference.label}}} never reached the document: the Pandoc reader dropped it',
                source_location.get((reference.command, reference.label), reference.location),
            )
        )
    return findings


def _split_keywords(texts: Any) -> tuple[str, ...] | None:
    """Keyword terms from the rendered keyword block(s): comma/semicolon separated, trailing period dropped."""
    blocks = _as_list(texts)
    if not blocks:
        return None
    terms = [term.strip().rstrip(".").strip() for block in blocks for term in re.split(r"[;,]", str(block))]
    return tuple(term for term in terms if term)


def _source_has_author(graph: Any) -> bool:
    from latex2word.latex_scan import iter_commands

    main = graph.files.get(graph.main)
    return main is not None and any(c.name == "author" for c in iter_commands(main.masked))


def _finish(
    result: BuildResult,
    config: ProjectConfig,
    profile: Profile,
    tool: PandocTool,
    workspace: Workspace | None,
    figures_run: FiguresRun | None,
    figure_plan: FigurePlan | None,
    bib_plan: BibliographyPlan | None,
    report: dict[str, Any] | None,
    pandoc_run: PandocRun | None,
    deliverable: Path | None,
    output: Path | None,
    keep_workspace: bool,
    *,
    records: list[Any] | None = None,
    changes: list[Any] | None = None,
    validation: DocxValidation | None = None,
    reference: ReferenceDoc | None = None,
    extraction: MetadataExtraction | None = None,
    metadata: DocumentMetadata | None = None,
    finalization: FinalizeResult | None = None,
    final_validation: DocxValidation | None = None,
    layout_validation: LayoutValidation | None = None,
    preliminary_path: Path | None = None,
) -> BuildResult:
    result.findings = _dedupe(result.findings)
    fail_on = Severity.WARNING if config.validation.fail_on == "warning" else Severity.ERROR
    blocking = [f for f in result.findings if f.severity is Severity.ERROR or (fail_on is Severity.WARNING and f.severity is Severity.WARNING)]
    result.ok = not blocking and deliverable is not None
    destination: Path | None = None
    output_sha: str | None = None
    if result.ok and deliverable is not None:
        destination = Path(output) if output is not None else config.output.directory / (
            config.output.filename or f"{config.project.main.stem}.docx"
        )
        destination = destination.resolve()
        _atomic_copy(deliverable, destination)
        result.output = destination
        output_sha = sha256_file(destination)
    counts = _counts(report, bib_plan, figure_plan) if report and bib_plan and figure_plan else {}
    result.summary = {
        "errors": len(result.errors),
        "warnings": len(result.warnings),
        "counts": counts,
        "numbering": _numbering_sequences(report) if report else None,
        "bibliography_mode": bib_plan.mode if bib_plan else None,
        "output_sha256": output_sha,
    }
    extras: dict[str, Any] = {}
    if workspace is not None and metadata is not None and finalization is not None:
        build_dir = workspace.path / BUILD_DIRNAME
        layout_report = build_layout_report(
            profile.id, profile.content_hash(), profile.layout, metadata, finalization, layout_validation
        )
        result.layout_report = build_dir / "layout_report.json"
        write_layout_report(result.layout_report, layout_report)
        if deliverable is not None and preliminary_path is not None:
            result.visual_review = build_dir / "visual_review.md"
            write_visual_review(
                result.visual_review,
                render_visual_review(
                    profile_id=profile.id, final_name=deliverable.name, preliminary_name=preliminary_path.name,
                    report=layout_report, findings=result.findings,
                ),
            )
        extras = _layout_manifest(
            profile, reference, extraction, metadata, preliminary_path, deliverable, final_validation, layout_report
        )
    if workspace is not None:
        manifest = _build_manifest(
            result, config, profile, tool, workspace, figures_run, figure_plan, bib_plan, report, pandoc_run, records,
            changes, validation, output_sha, extras,
        )
        result.manifest_path = workspace.path / "build_manifest.json"
        _write_json(result.manifest_path, manifest)
        if not result.ok and not config.workspace.keep_on_failure and not keep_workspace:
            workspace.clean()
            result.manifest_path = None
            result.workspace = None
    log_event(_log, logging.INFO, "build finished", ok=result.ok, errors=len(result.errors), warnings=len(result.warnings))
    return result


def _portable_pandoc(run: PandocRun, workspace: Path, project_root: Path) -> dict[str, Any]:
    """The Pandoc record for the manifest: absolute locations replaced by ``<workspace>`` / ``<project>``."""
    data = run.to_dict()

    def scrub(text: str) -> str:
        for prefix, token in ((workspace, "<workspace>"), (project_root, "<project>")):
            for form in (str(prefix), prefix.as_posix()):
                text = text.replace(form, token)
        return text

    data["argv"] = [scrub(str(a)) for a in data["argv"]]
    data["cwd"] = scrub(str(data["cwd"]))
    data["pandoc_path"] = Path(str(data["pandoc_path"])).name
    data["argv"][0] = Path(str(run.run.argv[0])).name
    data["stderr"] = scrub(str(data["stderr"]))
    data["warnings"] = [{**w, "text": scrub(w["text"])} for w in data["warnings"]]
    return data


def _relative_to_project(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _build_manifest(
    result: BuildResult,
    config: ProjectConfig,
    profile: Profile,
    tool: PandocTool,
    workspace: Workspace,
    figures_run: FiguresRun | None,
    figure_plan: FigurePlan | None,
    bib_plan: BibliographyPlan | None,
    report: dict[str, Any] | None,
    pandoc_run: PandocRun | None,
    records: list[Any] | None,
    changes: list[Any] | None,
    validation: DocxValidation | None,
    output_sha: str | None,
    extras: dict[str, Any],
) -> dict[str, Any]:
    ws = workspace.path
    staging_manifest = ws / "staging_manifest.json"
    figure_manifest = ws / "figures" / "figure_manifest.json"
    sources = {}
    if figures_run is not None and figures_run.staging is not None:
        sources = {f.source: f.sha256 for f in figures_run.staging.files}
    filters = [
        {"name": name, "sha256": sha256_file(_filter_dir() / name)} for name in FILTER_FILES if (_filter_dir() / name).is_file()
    ]
    return {
        "schema": BUILD_MANIFEST_SCHEMA,
        "schema_version": 1,
        "status": "ok" if result.ok else "failed",
        "latex2word_version": __version__,
        "pandoc_version": tool.version,
        "profile": {"id": profile.id, "chain": list(profile.chain), "display_name": profile.display_name},
        "source": {
            "project": config.project.name,
            "main": _relative_to_project(config.project.main, config.project.root),
            "file_hashes": sources,
        },
        "staging_manifest_sha256": sha256_file(staging_manifest) if staging_manifest.is_file() else None,
        "figure_manifest_sha256": sha256_file(figure_manifest) if figure_manifest.is_file() else None,
        "figures": [f.to_dict() for f in figure_plan.figures] if figure_plan else [],
        "bibliography": bib_plan.to_dict() if bib_plan else None,
        "filters": filters,
        "pandoc": _portable_pandoc(pandoc_run, workspace.path, config.project.root) if pandoc_run else None,
        "transformations": transformation_manifest(records or [], changes or [])["transformations"],
        "semantic_counts": result.summary.get("counts"),
        "numbering": result.summary.get("numbering"),
        "diagnostics": [f.to_dict() for f in result.findings],
        "output": {
            "file": result.output.name if result.output else None,
            "sha256": output_sha,
        },
        "docx_part_hashes": validation.metrics.get("part_hashes") if validation else None,
        "docx_metrics": {k: v for k, v in (validation.metrics.items() if validation else []) if k != "part_hashes"},
        **extras,
    }


def _layout_manifest(
    profile: Profile,
    reference: ReferenceDoc | None,
    extraction: MetadataExtraction | None,
    metadata: DocumentMetadata,
    preliminary: Path | None,
    final: Path | None,
    final_validation: DocxValidation | None,
    layout_report: dict[str, Any],
) -> dict[str, Any]:
    """The M4 part of the build manifest: everything needed to reproduce and audit the layout pass."""
    return {
        "profile_sha256": profile.content_hash(),
        "reference_docx": {**reference.to_dict(), "path": f"{BUILD_DIRNAME}/reference/{reference.path.name}"} if reference else None,
        "metadata": {
            "adapter": extraction.adapter if extraction else None,
            "counts": metadata.counts(),
            "consumed_commands": sorted(extraction.consumed_commands) if extraction else [],
            "consumed_environments": sorted(extraction.consumed_environments) if extraction else [],
        },
        "preliminary_docx": {"file": preliminary.name, "sha256": sha256_file(preliminary)} if preliminary else None,
        "final_docx": {
            "file": final.name,
            "sha256": sha256_file(final),
            "part_hashes": final_validation.metrics.get("part_hashes") if final_validation else None,
        } if final else None,
        "layout": {
            "page": layout_report["layout"]["page"],
            "columns": layout_report["layout"]["columns"],
            "sections": layout_report["sections"],
            "styles": layout_report["styles"],
            "figures": layout_report["figures"],
            "tables": layout_report["tables"],
            "equation_layout_mode": layout_report["equation_layout_mode"],
            "equations": layout_report["equations"],
            "frontmatter": layout_report["frontmatter"],
            "direct_format": layout_report["direct_format"],
        },
    }


def render_text(result: BuildResult, *, verbose: bool = False) -> str:
    lines = [f"Profile: {result.profile_id}"]
    numbering = result.summary.get("numbering")
    if numbering:
        sections = ", ".join(f"{s['text']}" for s in numbering["sections"])
        lines += [
            f"Sections:  {sections}",
            f"Unnumbered headings: {', '.join(numbering['unnumbered_headings']) or '-'}",
            f"Figures:   {', '.join(numbering['figures']) or '-'}",
            f"Tables:    {', '.join(numbering['tables']) or '-'}",
            f"Equations: {', '.join(numbering['equations']) or '-'}",
        ]
    counts = result.summary.get("counts") or {}
    if counts:
        lines.append(
            f"References: {counts['references']}   citation keys: {counts['citation_keys']}   "
            f"bibliography entries expected: {counts['bibliography_entries_expected']}"
        )
    shown = [f for f in result.findings if verbose or f.severity is not Severity.INFO]
    lines += ["", f"Diagnostics ({len(shown)}):", *[f"  {f}" for f in shown]]
    if result.workspace:
        lines.append(f"Workspace: {result.workspace}")
    if result.manifest_path:
        lines.append(f"Manifest:  {result.manifest_path}")
    for label, path in (("Preliminary", result.preliminary), ("Final", result.final), ("Layout report", result.layout_report),
                        ("Visual review", result.visual_review)):
        if path:
            lines.append(f"{label + ':':<14}{path}")
    if result.output:
        lines.append(f"Output:    {result.output}")
    lines += ["", f"Result: {'OK' if result.ok else 'FAILED'} ({len(result.errors)} error(s), {len(result.warnings)} warning(s))"]
    return "\n".join(lines)
