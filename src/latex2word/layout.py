"""The resolved, typed layout of a profile (publisher-neutral).

:func:`resolve_layout` turns the merged profile ``layout``/``frontmatter``/``reference_docx``
data (already schema-validated) into immutable dataclasses with physical
:class:`~latex2word.units.Length` values. Nothing here knows a publisher or a
document class: every number comes from the profile.

Geometry vocabulary (also used by the OOXML layer):

* ``text width``   - page width minus left and right margin (the full text area);
* ``column width`` - ``(text width - gap * (n - 1)) / n`` for ``n`` columns of a section;
* ``\\columnwidth`` and ``\\linewidth`` resolve to the column width of the section the
  float sits in (they differ only inside nested boxes, which are not modelled);
* ``\\textwidth`` resolves to the page text width (``textwidth_basis: page-text``) or to the
  column width (``textwidth_basis: column``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from latex2word.errors import ConfigIssue, ProfileError
from latex2word.units import Length, UnitError, parse_length

PAGE_SIZES: dict[str, tuple[Length, Length]] = {
    "letter": (Length.inches("8.5"), Length.inches("11")),
    "legal": (Length.inches("8.5"), Length.inches("14")),
    "a4": (Length.of("210", "mm"), Length.of("297", "mm")),
    "a5": (Length.of("148", "mm"), Length.of("210", "mm")),
}


@dataclass(frozen=True)
class SectionGeometry:
    """Columns of one section."""

    columns: int
    text_width: Length
    gap: Length

    @property
    def column_width(self) -> Length:
        gutters = self.gap.emu * (self.columns - 1)
        return Length((self.text_width.emu - gutters) // self.columns) if self.columns > 1 else self.text_width


@dataclass(frozen=True)
class PageGeometry:
    width: Length
    height: Length
    orientation: str
    top: Length
    bottom: Length
    left: Length
    right: Length
    header_distance: Length
    footer_distance: Length

    @property
    def text_width(self) -> Length:
        return self.width - self.left - self.right

    @property
    def text_height(self) -> Length:
        return self.height - self.top - self.bottom

    def to_dict(self) -> dict[str, Any]:
        return {
            "width_twips": self.width.twips, "height_twips": self.height.twips, "orientation": self.orientation,
            "margin_top_twips": self.top.twips, "margin_bottom_twips": self.bottom.twips,
            "margin_left_twips": self.left.twips, "margin_right_twips": self.right.twips,
            "header_twips": self.header_distance.twips, "footer_twips": self.footer_distance.twips,
            "text_width_in": round(self.text_width.in_, 6),
        }


@dataclass(frozen=True)
class TextStyle:
    """Properties of one style; ``None`` means "inherit"."""

    font: str | None = None
    size_pt: float | None = None
    bold: bool | None = None
    italic: bool | None = None
    color: str | None = None
    caps: str | None = None
    alignment: str | None = None
    before_pt: float | None = None
    after_pt: float | None = None
    line_spacing: float | None = None
    line_height: Length | None = None  # fixed baseline distance; wins over line_spacing
    line_rule: str | None = None  # "at-least" (default with line_height) | "exact"
    keep_next: bool | None = None
    keep_lines: bool | None = None
    widow_control: bool | None = None
    page_break_before: bool | None = None
    right_tab_leader: str | None = None  # right tab at the text width (contents entries)
    indent_left: Length | None = None
    indent_first_line: Length | None = None
    indent_hanging: Length | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TextStyle:
        values: dict[str, Any] = {}
        for item in fields(cls):
            if item.name in data:
                value = data[item.name]
                values[item.name] = parse_length(value) if item.name.startswith("indent_") or item.name == "line_height" else value
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if value is not None:
                out[item.name] = value.twips if isinstance(value, Length) else value
        return out


@dataclass(frozen=True)
class EquationLayout:
    alignment: str
    number_alignment: str
    mode: str
    number_width: Length


@dataclass(frozen=True)
class FigureLayout:
    columnwidth_basis: str
    textwidth_basis: str
    alignment: str
    caption_placement: str
    allow_overflow: bool


@dataclass(frozen=True)
class TableLayout:
    alignment: str
    width: str | Length  # "auto" | "column" | Length
    layout: str
    margin_top: Length
    margin_bottom: Length
    margin_left: Length
    margin_right: Length
    header_emphasis: str
    borders: str
    keep_rows: bool
    caption_placement: str
    min_font_pt: float | None


@dataclass(frozen=True)
class ParagraphDefaults:
    line_spacing: float
    before_pt: float
    after_pt: float
    line_height: Length | None = None
    line_rule: str = "at-least"


@dataclass(frozen=True)
class AuthorBlockLayout:
    layout: str
    max_columns: int
    show_affiliation: bool
    show_email: bool


@dataclass(frozen=True)
class LabelledBlock:
    """Abstract / keywords strategy: ``layout`` plus the profile-defined label and punctuation."""

    layout: str
    label: str
    separator: str
    environments: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()

    @property
    def run_in_text(self) -> str:
        return f"{self.label}{self.separator}"


@dataclass(frozen=True)
class FrontmatterConfig:
    metadata_adapter: str
    author_block: AuthorBlockLayout
    abstract: LabelledBlock
    keywords: LabelledBlock
    show_date: bool


@dataclass(frozen=True)
class ReferenceDocxConfig:
    mode: str  # generated | external
    path: str | None = None


# LaTeX's relative size commands for the standard 10/11/12 pt classes (size10/11/12.clo)
_LATEX_SIZES = {
    10: {"tiny": 5, "scriptsize": 7, "footnotesize": 8, "small": 9, "normalsize": 10, "large": 12, "Large": 14.4,
         "LARGE": 17.28, "huge": 20.74, "Huge": 24.88},
    11: {"tiny": 6, "scriptsize": 8, "footnotesize": 9, "small": 10, "normalsize": 10.95, "large": 12, "Large": 14.4,
         "LARGE": 17.28, "huge": 20.74, "Huge": 24.88},
    12: {"tiny": 6, "scriptsize": 8, "footnotesize": 10, "small": 10.95, "normalsize": 12, "large": 14.4,
         "Large": 17.28, "LARGE": 20.74, "huge": 24.88, "Huge": 24.88},
}


# \baselineskip of each size command (size10/11/12.clo), i.e. LaTeX's single-spaced line height for that size
_LATEX_BASELINES = {
    10: {"tiny": 6, "scriptsize": 8, "footnotesize": 9.5, "small": 11, "normalsize": 12, "large": 14, "Large": 18,
         "LARGE": 22, "huge": 25, "Huge": 30},
    11: {"tiny": 7, "scriptsize": 9.5, "footnotesize": 11, "small": 12, "normalsize": 13.6, "large": 14, "Large": 18,
         "LARGE": 22, "huge": 25, "Huge": 30},
    12: {"tiny": 7, "scriptsize": 9.5, "footnotesize": 12, "small": 13.6, "normalsize": 14.5, "large": 18,
         "Large": 22, "LARGE": 25, "huge": 30, "Huge": 30},
}


def latex_baselineskip(default: TextStyle | None, command: str) -> float | None:
    """LaTeX's single-spaced line height (pt) for a size command at the nearest class size (``None``: unknown)."""
    base = default.size_pt if default is not None and default.size_pt else 10
    table = _LATEX_BASELINES[min(_LATEX_BASELINES, key=lambda size: abs(size - base))]
    value = table.get(command)
    return float(value) if value is not None else None


def latex_font_sizes(default: TextStyle | None, overrides: Mapping[str, Any]) -> dict[str, float]:
    """Point size of each relative size command: the LaTeX table of the nearest class size, then profile overrides."""
    base = default.size_pt if default is not None and default.size_pt else 10
    table = _LATEX_SIZES[min(_LATEX_SIZES, key=lambda size: abs(size - base))]
    return {**{k: float(v) for k, v in table.items()}, **{k: float(v) for k, v in overrides.items()}}


PAGE_NUMBER_POSITIONS = ("none", "footer-center", "footer-left", "footer-right", "header-center", "header-left", "header-right")


@dataclass(frozen=True)
class PaginationLayout:
    """Word sections and page numbers (see ``layout.pagination`` in the profile schema)."""

    page_numbers: str = "none"
    follow_source: bool = True
    format: str = "decimal"  # Word ST_NumberFormat of the document start, or "none"
    start: int = 1
    suppress_first_page: bool = False

    @property
    def part(self) -> str | None:
        """``footer`` / ``header`` / None."""
        return None if self.page_numbers == "none" else self.page_numbers.split("-")[0]

    @property
    def alignment(self) -> str:
        return "center" if self.page_numbers == "none" else self.page_numbers.split("-")[1]

    def to_dict(self) -> dict[str, Any]:
        return {"page_numbers": self.page_numbers, "follow_source": self.follow_source, "format": self.format,
                "start": self.start, "suppress_first_page": self.suppress_first_page}


@dataclass(frozen=True)
class ResolvedLayout:
    page: PageGeometry
    frontmatter_columns: int
    body_columns: int
    column_gap: Length
    body_start: str
    paragraph: ParagraphDefaults
    typography: Mapping[str, TextStyle]
    equations: EquationLayout
    figures: FigureLayout
    tables: TableLayout
    pagination: PaginationLayout = PaginationLayout()
    contents_list_level: int = 1  # TOC level of list-of-figures/tables/custom-list entries
    font_sizes: Mapping[str, float] = field(default_factory=dict)  # LaTeX relative size commands -> pt

    @property
    def frontmatter_section(self) -> SectionGeometry:
        return SectionGeometry(self.frontmatter_columns, self.page.text_width, self.column_gap)

    @property
    def body_section(self) -> SectionGeometry:
        return SectionGeometry(self.body_columns, self.page.text_width, self.column_gap)

    @property
    def needs_section_break(self) -> bool:
        """A separate front-matter section is only needed when its column count differs from the body's."""
        return self.frontmatter_columns != self.body_columns

    def to_dict(self) -> dict[str, Any]:
        front, body = self.frontmatter_section, self.body_section
        return {
            "page": self.page.to_dict(),
            "columns": {
                "frontmatter": self.frontmatter_columns, "body": self.body_columns, "gap_twips": self.column_gap.twips,
                "frontmatter_column_width_in": round(front.column_width.in_, 6),
                "body_column_width_in": round(body.column_width.in_, 6),
            },
            "body_start": self.body_start,
            "typography": {role: style.to_dict() for role, style in sorted(self.typography.items())},
            "pagination": self.pagination.to_dict(),
        }


def _issue(path: str, message: str) -> ConfigIssue:
    return ConfigIssue(path, message)


def _length(data: Mapping[str, Any], key: str, path: str, issues: list[ConfigIssue]) -> Length:
    try:
        return parse_length(data[key])
    except (UnitError, KeyError) as exc:
        issues.append(_issue(f"{path}.{key}", str(exc)))
        return Length(0)


def resolve_layout(data: Mapping[str, Any]) -> ResolvedLayout:
    """Resolve ``data["layout"]`` (a merged, schema-valid profile) or raise :class:`ProfileError`."""
    raw = data["layout"]
    issues: list[ConfigIssue] = []
    page_raw = raw["page"]
    size = page_raw["size"]
    if isinstance(size, str):
        width, height = PAGE_SIZES[size]
    else:
        width, height = _length(size, "width", "layout.page.size", issues), _length(size, "height", "layout.page.size", issues)
    orientation = page_raw["orientation"]
    if orientation == "landscape" and height > width:
        width, height = height, width
    elif orientation == "portrait" and width > height:
        width, height = height, width
    margins = page_raw["margins"]
    page = PageGeometry(
        width, height, orientation,
        *(_length(margins, side, "layout.page.margins", issues) for side in ("top", "bottom", "left", "right")),
        _length(page_raw, "header_distance", "layout.page", issues), _length(page_raw, "footer_distance", "layout.page", issues),
    )
    columns = raw["columns"]
    gap = _length(columns, "gap", "layout.columns", issues)
    if not issues:
        if page.left + page.right >= page.width:
            issues.append(_issue("layout.page.margins", "left and right margins leave no text width"))
        if page.top + page.bottom >= page.height:
            issues.append(_issue("layout.page.margins", "top and bottom margins leave no text height"))
    if not issues:
        for key in ("frontmatter", "body"):
            count = columns[key]
            if gap.emu * (count - 1) >= page.text_width.emu:
                issues.append(_issue(f"layout.columns.{key}", f"{count} columns with a {gap} gap do not fit the {page.text_width} text width"))
    strategy = raw["body_start"]["strategy"]
    para = raw["paragraph"]
    typography = {role: TextStyle.from_mapping(style) for role, style in raw["typography"].items()}
    eq = raw["equations"]
    fig = raw["figures"]
    tab = raw["tables"]
    cm = tab["cell_margins"]
    pag = raw.get("pagination") or {}
    table_width: str | Length = tab["width"]
    if table_width not in ("auto", "column"):
        table_width = _length(tab, "width", "layout.tables", issues)
    layout = ResolvedLayout(
        page=page,
        frontmatter_columns=columns["frontmatter"],
        body_columns=columns["body"],
        column_gap=gap,
        body_start=strategy,
        paragraph=ParagraphDefaults(
            float(para["line_spacing"]), float(para["before_pt"]), float(para["after_pt"]),
            _length(para, "line_height", "layout.paragraph", issues) if "line_height" in para else None,
            str(para.get("line_rule", "at-least")),
        ),
        typography=typography,
        equations=EquationLayout(eq["alignment"], eq["number_alignment"], eq["mode"], _length(eq, "number_width", "layout.equations", issues)),
        figures=FigureLayout(fig["columnwidth_basis"], fig["textwidth_basis"], fig["alignment"], fig["caption_placement"], bool(fig["allow_overflow"])),
        tables=TableLayout(
            tab["alignment"], table_width, tab["layout"],
            *(_length(cm, side, "layout.tables.cell_margins", issues) for side in ("top", "bottom", "left", "right")),
            tab["header_emphasis"], tab["borders"], bool(tab["keep_rows"]), tab["caption_placement"], tab.get("min_font_pt"),
        ),
        contents_list_level=int((raw.get("contents") or {}).get("list_level", 1)),
        font_sizes=latex_font_sizes(typography.get("default"), raw.get("font_sizes") or {}),
        pagination=PaginationLayout(
            str(pag.get("page_numbers", "none")), bool(pag.get("follow_source", True)), str(pag.get("format", "decimal")),
            int(pag.get("start", 1)), bool(pag.get("suppress_first_page", False)),
        ),
    )
    if issues:
        raise ProfileError(f"{len(issues)} invalid layout value(s)", issues)
    return layout


def resolve_frontmatter(data: Mapping[str, Any]) -> FrontmatterConfig:
    raw = data["frontmatter"]
    block = raw["author_block"]
    abstract, keywords = raw["abstract"], raw["keywords"]
    return FrontmatterConfig(
        metadata_adapter=raw["metadata_adapter"],
        author_block=AuthorBlockLayout(block["layout"], int(block["max_columns"]), bool(block["show_affiliation"]), bool(block["show_email"])),
        abstract=LabelledBlock(abstract["layout"], abstract["label"], abstract["separator"]),
        keywords=LabelledBlock(
            keywords["layout"], keywords["label"], keywords["separator"],
            tuple(keywords["environments"]), tuple(keywords["commands"]),
        ),
        show_date=bool(raw["date"]["show"]),
    )


def resolve_reference_docx(data: Mapping[str, Any]) -> ReferenceDocxConfig:
    raw = data["reference_docx"]
    return ReferenceDocxConfig(raw["mode"], raw.get("path"))


# --------------------------------------------------------------------------- width resolution


@dataclass(frozen=True)
class ResolvedWidth:
    """A figure width resolved against the geometry of the section the figure is in."""

    requested: Length | None  # None: no width requested (intrinsic size)
    available: Length  # content width of the figure's column
    basis: str | None  # columnwidth | linewidth | textwidth | absolute | None


def basis_width(basis: str, section: SectionGeometry, layout: ResolvedLayout) -> Length:
    """The length one LaTeX width macro denotes in ``section`` (the three macros are never synonyms)."""
    if basis == "columnwidth":
        return section.column_width
    if basis == "linewidth":
        return section.column_width
    if basis == "textwidth":
        return layout.page.text_width if layout.figures.textwidth_basis == "page-text" else section.column_width
    raise ValueError(f"unknown width basis {basis!r}")


def resolve_figure_width(width: Mapping[str, Any] | None, section: SectionGeometry, layout: ResolvedLayout) -> ResolvedWidth:
    """Resolve a width IR (``kind``/``fraction``/``basis``/``inches``) against ``section``."""
    available = section.column_width
    if not width or width.get("kind") not in ("relative", "absolute"):
        return ResolvedWidth(None, available, None)
    if width["kind"] == "absolute":
        return ResolvedWidth(Length.inches(repr(float(width["inches"]))), available, "absolute")
    basis = str(width["basis"])
    return ResolvedWidth(basis_width(basis, section, layout).scaled(float(width["fraction"])), available, basis)


__all__ = [
    "PAGE_SIZES", "AuthorBlockLayout", "EquationLayout", "FigureLayout", "FrontmatterConfig", "LabelledBlock",
    "PageGeometry", "ParagraphDefaults", "ReferenceDocxConfig", "ResolvedLayout", "ResolvedWidth", "SectionGeometry",
    "TableLayout", "TextStyle", "basis_width", "resolve_frontmatter", "resolve_layout",
    "resolve_reference_docx", "resolve_figure_width",
]
