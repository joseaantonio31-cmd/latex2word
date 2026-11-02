"""Profile layout schema: typed dimensions, inheritance, validation, resolved geometry (publisher-neutral)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from latex2word.errors import ProfileError
from latex2word.layout import resolve_figure_width
from latex2word.profiles import load_profile
from latex2word.units import Length


def write_profile(directory: Path, name: str, data: dict, *, extends: str | None = "generic") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    body = {"profile_schema_version": 1, "id": name, **({"extends": extends} if extends else {}), **data}
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return path


def load(tmp_path: Path, layout: dict, **extra):
    write_profile(tmp_path, "custom", {"layout": layout, **extra})
    return load_profile("custom", [tmp_path])


def test_ieee_geometry_comes_from_the_profile_data_in_exact_twips():
    layout = load_profile("ieee-conference").layout
    page = layout.page
    assert (page.width.twips, page.height.twips) == (12240, 15840)
    assert (page.top.twips, page.bottom.twips, page.left.twips, page.right.twips) == (1080, 1440, 900, 900)
    assert (layout.frontmatter_columns, layout.body_columns, layout.column_gap.twips) == (1, 2, 360)
    assert layout.needs_section_break and layout.body_start == "first-numbered-heading"


def test_column_width_is_derived_from_the_text_area_not_hard_coded():
    layout = load_profile("ieee-conference").layout
    assert layout.page.text_width.in_ == pytest.approx(7.25)
    assert layout.body_section.column_width.in_ == pytest.approx((7.25 - 0.25) / 2)
    assert layout.frontmatter_section.column_width == layout.page.text_width


def test_columnwidth_linewidth_and_textwidth_are_distinct_bases():
    layout = load_profile("ieee-conference").layout
    section = layout.body_section
    column = resolve_figure_width({"kind": "relative", "fraction": 0.98, "basis": "columnwidth"}, section, layout)
    line = resolve_figure_width({"kind": "relative", "fraction": 0.98, "basis": "linewidth"}, section, layout)
    text = resolve_figure_width({"kind": "relative", "fraction": 0.5, "basis": "textwidth"}, section, layout)
    assert column.requested.in_ == pytest.approx(3.43) and line.requested == column.requested
    assert text.requested.in_ == pytest.approx(3.625) and text.requested > text.available  # half the page text > a column
    assert column.available == section.column_width


def test_one_column_layout_resolves_every_basis_to_the_text_width():
    layout = load_profile("generic").layout
    for basis in ("columnwidth", "linewidth", "textwidth"):
        resolved = resolve_figure_width({"kind": "relative", "fraction": 1.0, "basis": basis}, layout.body_section, layout)
        assert resolved.requested.in_ == pytest.approx(6.5)


def test_absolute_widths_in_every_unit_resolve_to_the_same_physical_length():
    layout = load_profile("generic").layout
    inches = 72 / 72
    for original, value in (("1in", inches), ("72pt", inches), ("25.4mm", inches), ("2.54cm", inches)):
        parsed = resolve_figure_width({"kind": "absolute", "inches": value, "original": original}, layout.body_section, layout)
        assert parsed.requested.in_ == pytest.approx(1.0)


def test_unresolvable_width_requests_nothing():
    layout = load_profile("generic").layout
    assert resolve_figure_width({"kind": "unsupported"}, layout.body_section, layout).requested is None
    assert resolve_figure_width(None, layout.body_section, layout).requested is None


def test_a_profile_inherits_layout_and_overrides_single_keys(tmp_path: Path):
    profile = load(tmp_path, {"page": {"size": "a4", "margins": {"left": "30mm"}}, "columns": {"body": 3}})
    layout = profile.layout
    assert (layout.page.width.twips, layout.page.height.twips) == (11906, 16838)
    assert layout.page.left == Length.of(30, "mm")
    assert layout.page.right == load_profile("generic").layout.page.right  # inherited
    assert layout.body_columns == 3 and layout.frontmatter_columns == 1
    assert layout.typography["default"].font == "Times New Roman"  # untouched branch survives the partial override


def test_landscape_swaps_the_paper_dimensions(tmp_path: Path):
    layout = load(tmp_path, {"page": {"orientation": "landscape"}}).layout
    assert layout.page.width > layout.page.height and layout.page.orientation == "landscape"


def test_a_root_profile_without_extends_still_resolves_a_complete_layout(tmp_path: Path):
    generic = load_profile("generic")
    write_profile(tmp_path, "standalone", {k: v for k, v in generic.data.items() if k in ("required_tools", "optional_tools", "numbering", "bibliography", "pandoc", "tex")}, extends=None)
    standalone = load_profile("standalone", [tmp_path])
    assert standalone.layout.page.to_dict() == generic.layout.page.to_dict()


def test_unknown_layout_keys_fail_schema_validation(tmp_path: Path):
    for layout in ({"colour": "red"}, {"page": {"bleed": "3mm"}}, {"typography": {"headline": {"size_pt": 12}}}, {"typography": {"title": {"fontsize": 12}}}):
        with pytest.raises(ProfileError):
            load(tmp_path, layout)


@pytest.mark.parametrize(
    "layout",
    [
        {"page": {"margins": {"top": "1"}}},
        {"page": {"margins": {"top": "1furlong"}}},
        {"page": {"margins": {"top": "-1in"}}},
        {"page": {"margins": {"top": 1}}},
        {"page": {"size": "folio"}},
        {"columns": {"body": 0}},
        {"columns": {"body": 7}},
        {"columns": {"body": 2.5}},
        {"columns": {"gap": "wide"}},
        {"typography": {"title": {"size_pt": 2}}},
        {"typography": {"title": {"alignment": "centre"}}},
        {"typography": {"title": {"color": "red"}}},
        {"typography": {"title": {"indent_left": "3"}}},
        {"equations": {"mode": "magic"}},
        {"tables": {"borders": "fancy"}},
        {"body_start": {"strategy": "after-the-word-Introduction"}},
    ],
)
def test_invalid_layout_values_are_rejected(tmp_path: Path, layout: dict):
    with pytest.raises(ProfileError):
        load(tmp_path, layout)


def test_margins_that_leave_no_text_area_are_rejected(tmp_path: Path):
    with pytest.raises(ProfileError, match="invalid layout"):
        load(tmp_path, {"page": {"margins": {"left": "4.25in", "right": "4.25in"}}})
    with pytest.raises(ProfileError, match="invalid layout"):
        load(tmp_path, {"page": {"margins": {"top": "6in", "bottom": "5in"}}})


def test_columns_that_do_not_fit_the_text_width_are_rejected(tmp_path: Path):
    with pytest.raises(ProfileError, match="invalid layout"):
        load(tmp_path, {"columns": {"body": 6, "gap": "2in"}})


def test_unknown_metadata_adapter_is_a_profile_error(tmp_path: Path):
    write_profile(tmp_path, "custom", {"frontmatter": {"metadata_adapter": "no-such-adapter"}})
    with pytest.raises(ProfileError) as caught:
        load_profile("custom", [tmp_path])
    assert any("no-such-adapter" in issue.message for issue in caught.value.issues)


def test_external_reference_document_must_exist_and_resolves_next_to_the_profile(tmp_path: Path):
    write_profile(tmp_path, "custom", {"reference_docx": {"mode": "external", "path": "template.docx"}})
    with pytest.raises(ProfileError, match="does not exist"):
        load_profile("custom", [tmp_path])
    (tmp_path / "template.docx").write_bytes(b"PK")
    profile = load_profile("custom", [tmp_path])
    assert profile.reference_docx.mode == "external" and profile.reference_docx_path == (tmp_path / "template.docx").resolve()


def test_generated_mode_rejects_a_path(tmp_path: Path):
    write_profile(tmp_path, "custom", {"reference_docx": {"mode": "generated", "path": "x.docx"}})
    with pytest.raises(ProfileError, match="only valid with mode external"):
        load_profile("custom", [tmp_path])


def test_external_mode_requires_a_path(tmp_path: Path):
    write_profile(tmp_path, "custom", {"reference_docx": {"mode": "external"}})
    with pytest.raises(ProfileError, match="no path"):
        load_profile("custom", [tmp_path])


def test_the_profile_hash_changes_with_layout_and_not_with_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base = load_profile("ieee-conference").content_hash()
    monkeypatch.chdir(tmp_path)
    assert load_profile("ieee-conference").content_hash() == base
    assert load_profile("generic").content_hash() != base
    changed = load(tmp_path, {"page": {"margins": {"left": "0.7in"}}})
    assert changed.content_hash() != load_profile("generic").content_hash()


def test_semantic_profiles_without_layout_keys_are_unchanged(tmp_path: Path):
    profile = load_profile("alt-numbering", [Path(__file__).parent / "fixtures" / "semantic" / "profiles"])
    assert profile.layout.body_columns == 1 and profile.numbering["figures"]["format"] == "Plate {n}"
