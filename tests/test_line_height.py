"""Fixed line heights (TeX \\baselineskip semantics) in profiles: line_height + line_rule -> w:spacing."""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from latex2word.errors import ProfileError
from latex2word.ooxml.namespaces import w
from latex2word.ooxml.styles import apply_typography
from latex2word.profiles import load_profile

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _profile(tmp_path: Path, layout_yaml: str):
    directory = tmp_path / "profiles"
    directory.mkdir(exist_ok=True)
    (directory / "lh.yaml").write_text(
        "profile_schema_version: 1\nid: lh\nextends: generic\nlayout:\n" + layout_yaml, encoding="utf-8"
    )
    return load_profile("lh", [directory])


def _styles(layout) -> etree._Element:
    styles = etree.fromstring(f'<w:styles xmlns:w="{W_NS}"/>'.encode())
    apply_typography(styles, layout)
    return styles


def _spacing_of(styles: etree._Element, style_id: str) -> dict[str, str]:
    for style in styles.iter(w("style")):
        if style.get(w("styleId")) == style_id:
            spacing = style.find(f"{w('pPr')}/{w('spacing')}")
            return {k.split("}")[1]: v for k, v in spacing.attrib.items()} if spacing is not None else {}
    raise AssertionError(f"style {style_id} not found")


def test_paragraph_line_height_is_an_at_least_baseline_distance_in_doc_defaults_and_body(tmp_path: Path):
    profile = _profile(tmp_path, "  paragraph: {line_spacing: 1.5, line_height: 18pt, before_pt: 0, after_pt: 0}\n")
    styles = _styles(profile.layout)
    defaults = styles.find(f"{w('docDefaults')}/{w('pPrDefault')}/{w('pPr')}/{w('spacing')}")
    assert defaults.get(w("line")) == "360" and defaults.get(w("lineRule")) == "atLeast"  # 18 pt = 360 twips
    body = _spacing_of(styles, "BodyText")
    assert body["line"] == "360" and body["lineRule"] == "atLeast"  # line_height wins over line_spacing: 1.5


def test_style_line_height_with_exact_rule_and_multiple_still_supported(tmp_path: Path):
    profile = _profile(
        tmp_path,
        "  typography:\n"
        "    figure_caption: {size_pt: 10, line_height: 12pt, line_rule: exact}\n"
        "    table_text: {size_pt: 10, line_spacing: 1.0}\n",
    )
    styles = _styles(profile.layout)
    caption = _spacing_of(styles, "ImageCaption")
    assert caption["line"] == "240" and caption["lineRule"] == "exact"
    compact = _spacing_of(styles, "Compact")
    assert compact["line"] == "240" and compact["lineRule"] == "auto"


@pytest.mark.parametrize(
    "layout_yaml",
    [
        "  paragraph: {line_spacing: 1.0, line_height: 18pt, line_rule: sometimes, before_pt: 0, after_pt: 0}\n",
        "  typography:\n    body: {line_height: tall}\n",
    ],
)
def test_invalid_line_height_values_are_rejected(tmp_path: Path, layout_yaml: str):
    with pytest.raises(ProfileError):
        _profile(tmp_path, layout_yaml)
