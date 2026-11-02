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
                references.extend(LabelReference(label, name, where) for label in split_top_level(content))
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
