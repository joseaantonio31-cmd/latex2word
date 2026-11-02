"""Expand ``\\newcolumntype`` column types in table preambles of the staged copy.

Pandoc's LaTeX reader understands the standard column specifiers (``l c r p m b
X``, ``@{..}``, ``>{..}``, ``<{..}``, ``|``) but not types defined with the
``array`` package's ``\\newcolumntype``: a ``longtable`` or ``tabularx`` whose
preamble uses such a type is not read as a table at all (its cells, caption and
label degrade to a paragraph of text). TeX expands the type before the table is
built; this pass does the same textual expansion in the staged copy, so the
table survives with its semantics. The original definitions stay in the
preamble (Pandoc ignores them).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from latex2word.diagnostics import Severity
from latex2word.latex_scan import iter_commands, read_group, read_optional, scan_environments, skip_ws
from latex2word.rewrite import Edit
from latex2word.source_graph import Finding, SourceGraph, SourceLocation

# environment -> number of mandatory groups before the column specification
TABLE_ENVIRONMENTS: dict[str, int] = {
    "tabular": 0,
    "array": 0,
    "longtable": 0,
    "tabular*": 1,
    "tabularx": 1,
    "tabulary": 1,
    "xltabular": 1,
}
# specifiers whose following brace group is an argument, never a column type
_TAKES_GROUP = frozenset("@!<>pmbwW")
_MAX_DEPTH = 16


@dataclass(frozen=True)
class ColumnType:
    name: str
    nargs: int
    body: str
    location: SourceLocation


@dataclass
class ColumnTypePlan:
    edits: list[Edit] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    types: dict[str, ColumnType] = field(default_factory=dict)


def collect_column_types(graph: SourceGraph) -> tuple[dict[str, ColumnType], list[Finding]]:
    """``\\newcolumntype{X}[n]{body}`` definitions of all scanned files (a later one wins, as in TeX)."""
    types: dict[str, ColumnType] = {}
    findings: list[Finding] = []
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked, text = scanned.masked, scanned.text
        for command in iter_commands(masked):
            if command.name != "newcolumntype":
                continue
            where = SourceLocation(rel, scanned.lines.line(command.start))
            name_group = read_group(masked, skip_ws(masked, command.end))
            if name_group is None:
                findings.append(Finding("L2W-W-TAB-001", Severity.WARNING,
                                        "\\newcolumntype without a braced name is not expanded", where))
                continue
            name = text[name_group[0] : name_group[1]].strip()
            pos = skip_ws(masked, name_group[2])
            nargs = 0
            optional = read_optional(masked, pos)
            if optional is not None:
                count = text[optional[0] : optional[1]].strip()
                if not count.isdigit():
                    findings.append(Finding("L2W-W-TAB-001", Severity.WARNING,
                                            f"\\newcolumntype{{{name}}}: argument count {count!r} is not a number", where))
                    continue
                nargs = int(count)
                pos = skip_ws(masked, optional[2])
            body = read_group(masked, pos)
            if body is None or len(name) != 1:
                findings.append(Finding("L2W-W-TAB-001", Severity.WARNING,
                                        f"\\newcolumntype{{{name}}} could not be read; tables using it may not convert", where))
                continue
            types[name] = ColumnType(name, nargs, text[body[0] : body[1]], where)
    return types, findings


def _substitute(body: str, args: list[str]) -> str:
    out: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == "#" and i + 1 < len(body) and body[i + 1].isdigit():
            index = int(body[i + 1]) - 1
            out.append(args[index] if 0 <= index < len(args) else "")
            i += 2
            continue
        out.append(body[i])
        i += 1
    return "".join(out)


def expand_spec(spec: str, types: dict[str, ColumnType], depth: int = 0) -> str:
    """``spec`` with every custom column type expanded (recursively); raises ValueError on runaway recursion."""
    if depth > _MAX_DEPTH:
        raise ValueError("column type expansion does not terminate")
    out: list[str] = []
    i = 0
    n = len(spec)
    while i < n:
        ch = spec[i]
        if ch == "\\":  # a control sequence (e.g. \vline) is copied whole
            j = i + 1
            while j < n and spec[j].isalpha():
                j += 1
            j = max(j, i + 2)
            out.append(spec[i:j])
            i = j
            continue
        if ch == "{":
            group = read_group(spec, i)
            if group is None:
                raise ValueError("unbalanced braces in column specification")
            out.append(spec[i : group[2]])
            i = group[2]
            continue
        if ch in _TAKES_GROUP and ch not in types:
            out.append(ch)
            i += 1
            j = skip_ws(spec, i)
            group = read_group(spec, j)
            if group is not None:
                out.append(spec[i : group[2]])
                i = group[2]
            continue
        if ch in types:
            definition = types[ch]
            pos = i + 1
            args: list[str] = []
            for _ in range(definition.nargs):
                pos = skip_ws(spec, pos)
                group = read_group(spec, pos)
                if group is None:
                    raise ValueError(f"column type {ch!r} expects {definition.nargs} argument(s)")
                args.append(spec[group[0] : group[1]])
                pos = group[2]
            out.append(expand_spec(_substitute(definition.body, args), types, depth + 1))
            i = pos
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_LONGTABLE_MARKERS = ("endfirsthead", "endhead", "endfoot", "endlastfoot")


def _simplify_multicolumns_in(text: str, types: dict) -> str:
    """``text`` with every multicolumn spec reduced (for rows that are moved as text)."""
    out, last = [], 0
    for command in iter_commands(text):
        if command.name != "multicolumn":
            continue
        count = read_group(text, skip_ws(text, command.end))
        spec = read_group(text, skip_ws(text, count[2])) if count else None
        if spec is None:
            continue
        simple = simplify_cell_spec(text[spec[0] : spec[1]], types)
        if simple is not None:
            out.append(text[last : spec[0]] + simple)
            last = spec[1]
    out.append(text[last:])
    return "".join(out)


def plan_longtable_rewrites(graph: SourceGraph) -> list[Edit]:
    """Drop the per-page continuation head and foot of every ``longtable``.

    Pandoc's reader puts the rows of ``\\endhead`` (when ``\\endfirsthead`` also
    exists) and of ``\\endfoot`` (when ``\\endlastfoot`` also exists) into the
    table body, so a "continued" header and a "continued on the next page" note
    become content rows. They only exist for page breaks, which Word handles
    with its own repeated header row.
    """
    edits: list[Edit] = []
    types, _ = collect_column_types(graph)
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked = scanned.masked
        environments, _ = scan_environments(masked, ("longtable",))
        for env in environments:
            markers = [
                (c.name, c.start, c.end)
                for c in iter_commands(masked[: env.content_end])
                if c.start >= env.content_start and c.name in _LONGTABLE_MARKERS
            ]
            names = [name for name, _, _ in markers]
            previous_end = None
            for name, start, end in markers:
                drop = (name == "endhead" and "endfirsthead" in names) or (name == "endfoot" and "endlastfoot" in names)
                # the final foot (\endlastfoot, or \endfoot when there is no last foot) is printed under the last
                # row; Pandoc would put its rows at the top of the body, so they move to the end of the body
                move = name == "endlastfoot" or (name == "endfoot" and "endlastfoot" not in names)
                if drop and previous_end is not None:
                    edits.append(Edit(rel, previous_end, end, "", "longtable-continuation",
                                      f"longtable per-page continuation block (\\{name}) removed: Pandoc would "
                                      "turn it into body rows", "longtable"))
                elif move and previous_end is not None:
                    rows = _simplify_multicolumns_in(scanned.text[previous_end:start], types)
                    if rows.strip():
                        body = masked[end : env.content_end].rstrip()
                        joiner = "" if body.endswith("\\\\") or not body else "\\\\"
                        edits.append(Edit(rel, previous_end, end, "", "longtable-foot",
                                          f"longtable final foot (\\{name}) moved after the last row, where it is printed",
                                          "longtable"))
                        edits.append(Edit(rel, env.content_end, env.content_end, f"{joiner} {rows.strip()}\n",  # no blank line: \par ends the table
                                          "longtable-foot", "longtable final foot rows placed after the last row",
                                          "longtable"))
                previous_end = end
    return edits


_SPEC_DECORATIONS = frozenset("@!<>")
_SPEC_TYPES_WITH_ARG = frozenset("pmbwW")


def simplify_cell_spec(spec: str, types: dict[str, ColumnType]) -> str | None:
    """The single column type of a ``\\multicolumn`` spec without ``@{}``, ``!{}``, ``>{}``, ``<{}`` and rules.

    Pandoc's reader loses a ``\\multicolumn`` cell (or the whole table) when the spec carries such
    decorations (``@{}p{\\textwidth}@{}``); they only adjust TeX's spacing. None when the spec does not
    reduce to exactly one column.
    """
    try:
        expanded = expand_spec(spec, types)
    except ValueError:
        return None
    columns: list[str] = []
    i, n = 0, len(expanded)
    while i < n:
        ch = expanded[i]
        if ch.isspace() or ch == "|":
            i += 1
        elif ch in _SPEC_DECORATIONS:
            group = read_group(expanded, skip_ws(expanded, i + 1))
            if group is None:
                return None
            i = group[2]
        elif ch in _SPEC_TYPES_WITH_ARG:
            group = read_group(expanded, skip_ws(expanded, i + 1))
            if group is None:
                return None
            columns.append(expanded[i : group[2]])
            i = group[2]
        elif ch in "lcrX":
            columns.append(ch)
            i += 1
        else:
            return None
    return columns[0] if len(columns) == 1 else None


def plan_multicolumn_rewrites(graph: SourceGraph, types: dict[str, ColumnType] | None = None) -> list[Edit]:
    """Edits reducing ``\\multicolumn{n}{spec}{..}`` specs to their single column type (see :func:`simplify_cell_spec`)."""
    if types is None:
        types, _ = collect_column_types(graph)
    edits: list[Edit] = []
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked, text = scanned.masked, scanned.text
        for command in iter_commands(masked):
            if command.name != "multicolumn":
                continue
            count = read_group(masked, skip_ws(masked, command.end))
            spec = read_group(masked, skip_ws(masked, count[2])) if count else None
            if spec is None:
                continue
            original = text[spec[0] : spec[1]]
            simple = simplify_cell_spec(original, types)
            if simple is not None and simple != original.strip():
                edits.append(Edit(rel, spec[0], spec[1], simple, "column-type",
                                  "\\multicolumn spec reduced to its column type (Pandoc loses decorated cells)",
                                  "multicolumn"))
    return edits


def plan_column_type_rewrites(graph: SourceGraph) -> ColumnTypePlan:
    """Edits that replace table column specifications using custom types by their expansion."""
    plan = ColumnTypePlan()
    plan.types, plan.findings = collect_column_types(graph)
    for rel in graph.tex_order:
        scanned = graph.files.get(rel)
        if scanned is None:
            continue
        masked, text = scanned.masked, scanned.text
        for command in iter_commands(masked):
            if command.name != "begin":
                continue
            name_group = read_group(masked, skip_ws(masked, command.end))
            if name_group is None:
                continue
            environment = masked[name_group[0] : name_group[1]].strip()
            if environment not in TABLE_ENVIRONMENTS:
                continue
            pos = name_group[2]
            ok = True
            for _ in range(TABLE_ENVIRONMENTS[environment]):
                group = read_group(masked, skip_ws(masked, pos))
                if group is None:
                    ok = False
                    break
                pos = group[2]
            if not ok:
                continue
            optional = read_optional(masked, skip_ws(masked, pos))
            if optional is not None:
                pos = optional[2]
            spec_group = read_group(masked, skip_ws(masked, pos))
            if spec_group is None:
                continue
            original = text[spec_group[0] : spec_group[1]]
            where = SourceLocation(rel, scanned.lines.line(command.start))
            try:
                expanded = expand_spec(original, plan.types)
            except ValueError as exc:
                plan.findings.append(Finding("L2W-W-TAB-002", Severity.WARNING,
                                             f"{environment} column specification {original!r}: {exc}; not expanded", where))
                continue
            reason = "\\newcolumntype column types expanded (Pandoc does not read custom column types)"
            closing = masked.find("\\end{" + environment + "}", spec_group[2])
            body = masked[spec_group[2] : closing if closing >= 0 else len(masked)]
            if "\\multicolumn" in body:
                stripped = strip_cell_decorations(expanded)
                if stripped != expanded:
                    # with >{..}/<{..} in the table preamble Pandoc silently drops every \multicolumn row
                    expanded = stripped
                    reason = ("cell decorations >{..}/<{..} removed from a table with \\multicolumn rows (Pandoc "
                              "drops those rows otherwise); custom column types expanded")
            if expanded != original:
                plan.edits.append(Edit(rel, spec_group[0], spec_group[1], expanded, "column-type", reason, environment))
    return plan


def strip_cell_decorations(spec: str) -> str:
    """``spec`` without ``>{..}`` / ``<{..}`` groups (cell formatting only; ``@{..}`` and widths are kept)."""
    out: list[str] = []
    i, n = 0, len(spec)
    while i < n:
        ch = spec[i]
        if ch in "<>":
            group = read_group(spec, skip_ws(spec, i + 1))
            if group is not None:
                i = group[2]
                continue
        if ch == "{":
            group = read_group(spec, i)
            if group is not None:
                out.append(spec[i : group[2]])
                i = group[2]
                continue
        out.append(ch)
        i += 1
    return "".join(out)
