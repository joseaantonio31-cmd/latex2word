"""Labels and references in the LaTeX source, with locations.

The Pandoc AST carries no source positions, so the label model is cross-checked
here against the live source text: where every ``\\label`` is defined, where it is
referenced, duplicates, dangling references and targets nobody references.
"""

from __future__ import annotations

from dataclasses import dataclass

from latex2word.diagnostics import Severity
from latex2word.latex_scan import iter_commands, read_group, read_optional, skip_ws, split_top_level
from latex2word.source_graph import Finding, SourceGraph, SourceLocation

REFERENCE_COMMANDS = frozenset(
    {"ref", "eqref", "autoref", "cref", "Cref", "vref", "Vref", "pageref", "nameref", "Nameref", "labelcref",
     "crefrange", "Crefrange", "cpageref", "Cpageref"}
)


@dataclass(frozen=True)
class LabelDefinition:
    label: str
    location: SourceLocation


@dataclass(frozen=True)
class LabelReference:
    label: str
    command: str
    location: SourceLocation
    in_definition: bool = False  # inside a macro/environment definition body: text only until the macro is used


_DEFINITION_COMMANDS = frozenset(
    {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand", "newenvironment", "renewenvironment",
     "def", "gdef", "edef", "xdef"}
)


def _definition_spans(masked: str) -> list[tuple[int, int]]:
    """Offsets ``(start, end)`` of macro/environment definitions (head and body groups)."""
    spans: list[tuple[int, int]] = []
    for command in iter_commands(masked):
        if command.name not in _DEFINITION_COMMANDS:
            continue
        pos = skip_ws(masked, command.end)
        if command.name.endswith("def"):  # \def\name<parameter text>{body}
            brace = masked.find("{", pos)
            group = read_group(masked, brace) if brace >= 0 else None
            if group is not None:
                spans.append((command.start, group[2]))
            continue
        end = None
        groups = 0
        while pos < len(masked) and groups < (3 if "environment" in command.name else 2):
            if masked[pos] == "[":
                optional = read_optional(masked, pos)
                if optional is None:
                    break
                pos = skip_ws(masked, optional[2])
                continue
            if masked[pos] == "\\" and groups == 0:  # \newcommand\name without braces
                name_end = pos + 1
                while name_end < len(masked) and masked[name_end].isalpha():
                    name_end += 1
                pos, groups = skip_ws(masked, name_end), 1
                continue
            group = read_group(masked, pos)
            if group is None:
                break
            end, groups = group[2], groups + 1
            pos = skip_ws(masked, group[2])
        if end is not None:
            spans.append((command.start, end))
    return spans


@dataclass(frozen=True)
class LabelScan:
    definitions: tuple[LabelDefinition, ...]
    references: tuple[LabelReference, ...]

    @property
    def names(self) -> list[str]:
        seen: dict[str, None] = {}
        for definition in self.definitions:
            seen.setdefault(definition.label, None)
        return list(seen)


def scan_labels(graph: SourceGraph) -> LabelScan:
    definitions: list[LabelDefinition] = []
    references: list[LabelReference] = []
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked = scanned.masked
        spans = _definition_spans(masked)
        for command in iter_commands(masked):
            name = command.name
            if name != "label" and name not in REFERENCE_COMMANDS:
                continue
            pos = skip_ws(masked, command.end)
            while masked[pos : pos + 1] == "[":
                group = read_optional(masked, pos)
                if group is None:
                    break
                pos = skip_ws(masked, group[2])
            group = read_group(masked, pos)
            if group is None:
                continue
            where = SourceLocation(rel, scanned.lines.line(command.start))
            content = " ".join(masked[group[0] : group[1]].split())
            if name == "label":
                definitions.append(LabelDefinition(content, where))
            else:
                inside = any(a < command.start < b for a, b in spans)
                references.extend(LabelReference(label, name, where, inside) for label in split_top_level(content))
    return LabelScan(tuple(definitions), tuple(references))


def check_labels(scan: LabelScan) -> list[Finding]:
    """Duplicate and dangling labels (errors) and unreferenced targets (one info)."""
    findings: list[Finding] = []
    first: dict[str, LabelDefinition] = {}
    for definition in scan.definitions:
        previous = first.get(definition.label)
        if previous is None:
            first[definition.label] = definition
            continue
        findings.append(
            Finding(
                "L2W-E-LABEL-001", Severity.ERROR,
                f'duplicate label "{definition.label}" (first defined at {previous.location})', definition.location,
            )
        )
    reported: set[tuple[str, str, int]] = set()
    for reference in scan.references:
        if reference.label in first:
            continue
        key = (reference.label, reference.location.file, reference.location.line)
        if key in reported:
            continue
        reported.add(key)
        findings.append(
            Finding(
                "L2W-E-REF-001", Severity.ERROR,
                f'reference to undefined label "{reference.label}" (\\{reference.command})', reference.location,
            )
        )
    referenced = {r.label for r in scan.references}
    unreferenced = [name for name in first if name not in referenced]
    if unreferenced:
        findings.append(
            Finding("L2W-I-LABEL-003", Severity.INFO, f"{len(unreferenced)} label(s) never referenced: {', '.join(unreferenced)}")
        )
    return findings


# ------------------------------------------------------------------ per-occurrence reference markers (audit F-04)

# Reference commands that Pandoc's LaTeX reader turns into links (\pageref/\nameref are reported by the probe).
LINKED_REFERENCE_COMMANDS = frozenset({"ref", "eqref", "autoref", "cref", "Cref", "vref"})
OCCURRENCE_PREFIX = "l2wocc"


@dataclass(frozen=True)
class ReferenceOccurrence:
    """One label inside one reference command of the source, with its own identity."""

    id: int
    command: str
    label: str
    location: SourceLocation


def plan_reference_markers(graph: SourceGraph) -> tuple[list, list[ReferenceOccurrence]]:
    """Staged-copy edits that write every reference label as ``l2wocc<n>:<label>``.

    The filter strips the prefix and reports occurrence ``n`` for every reference that reached the AST, so the
    build can detect the loss of a single occurrence even when other references to the same label survive.
    Not marked (and therefore not checked): references inside macro/environment definition bodies (only text until
    the macro is used), labels that are not literal (``#1``, macros), and arguments whose text differs from its
    comment-masked form.
    """
    from latex2word.rewrite import Edit  # local import: rewrite imports source_graph like this module

    edits: list[Edit] = []
    occurrences: list[ReferenceOccurrence] = []
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked, text = scanned.masked, scanned.text
        spans = _definition_spans(masked)
        for command in iter_commands(masked):
            if command.name not in LINKED_REFERENCE_COMMANDS or any(a < command.start < b for a, b in spans):
                continue
            pos = skip_ws(masked, command.end)
            while masked[pos : pos + 1] == "[":
                optional = read_optional(masked, pos)
                if optional is None:
                    break
                pos = skip_ws(masked, optional[2])
            group = read_group(masked, pos)
            if group is None or masked[group[0] : group[1]] != text[group[0] : group[1]]:
                continue
            labels = split_top_level(" ".join(text[group[0] : group[1]].split()))
            if not labels or any(any(ch in label for ch in "\\#{}") or label.startswith(OCCURRENCE_PREFIX) for label in labels):
                continue
            where = SourceLocation(rel, scanned.lines.line(command.start))
            marked = []
            for label in labels:
                occurrence = ReferenceOccurrence(len(occurrences) + 1, command.name, label, where)
                occurrences.append(occurrence)
                marked.append(f"{OCCURRENCE_PREFIX}{occurrence.id}:{label}")
            edits.append(
                Edit(rel, group[0], group[1], ",".join(marked), "reference-marker",
                     "occurrence identity for the per-occurrence reference check (audit F-04)",
                     ",".join(str(o.id) for o in occurrences[-len(labels):]))
            )
    return edits, occurrences

