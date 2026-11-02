from __future__ import annotations

from pathlib import Path

import pytest
from projectkit import GOOD_SVG

from latex2word.svg_validate import parse_length, validate_svg_bytes, validate_svg_file


def codes(report) -> set[str]:
    return {i.code for i in report.issues}


def svg(body: str = '<path d="M0 0L1 1"/>', attrs: str = 'width="10pt" height="5pt" viewBox="0 0 10 5"') -> bytes:
    return f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" {attrs}>{body}</svg>'.encode()


def test_valid_svg_records_metrics():
    report = validate_svg_bytes(
        svg('<path d="M0 0L1 1"/><path d="M1 1L2 2"/><text font-family="Foo, \'Bar\'">x</text>')
    )
    assert report.valid and not report.issues
    m = report.metrics
    assert (m.width, m.height, m.view_box) == ("10pt", "5pt", "0 0 10 5")
    assert (m.paths, m.text, m.images) == (2, 1, 0)
    assert m.fonts == ("Bar", "Foo")
    assert (m.width_px, m.height_px) == (13.3333, 6.6667)
    assert m.path_dominated is True
    assert report.sha256


def test_malformed_xml_is_an_error():
    report = validate_svg_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'><path></svg>")
    assert not report.valid and "malformed-xml" in codes(report) and report.metrics is None


def test_forbidden_control_character_is_reported_separately():
    report = validate_svg_bytes(svg('<path d="M0 0L1 1"/><desc>a\x1bb</desc>'))
    assert not report.valid and "control-characters" in codes(report)


def test_allowed_whitespace_controls_are_fine():
    assert validate_svg_bytes(svg('<path d="M0 0L1 1"/>\n\t\r<desc>x</desc>')).valid


def test_missing_svg_root():
    report = validate_svg_bytes(b"<html><body/></html>")
    assert codes(report) == {"missing-svg-root"}


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ("", {"missing-dimensions"}),
        ('width="10"', {"missing-dimensions"}),
        ('width="abc" height="5" viewBox="0 0 1 1"', {"invalid-dimensions"}),
        ('width="0" height="5" viewBox="0 0 1 1"', {"invalid-dimensions"}),
        ('viewBox="0 0 -1 5"', {"invalid-viewbox"}),
        ('viewBox="0 0 1"', {"invalid-viewbox"}),
    ],
)
def test_missing_or_invalid_dimensions(attrs: str, expected: set[str]):
    report = validate_svg_bytes(svg(attrs=attrs))
    assert not report.valid and expected <= codes(report)


def test_viewbox_only_and_size_only_are_warnings():
    only_box = validate_svg_bytes(svg(attrs='viewBox="0 0 10 5"'))
    only_size = validate_svg_bytes(svg(attrs='width="10" height="5"'))
    assert only_box.valid and codes(only_box) == {"missing-size"}
    assert only_size.valid and codes(only_size) == {"missing-viewbox"}
    assert (only_box.metrics.width_px, only_box.metrics.height_px) == (10.0, 5.0)


@pytest.mark.parametrize("body", ["", "<g/>", '<path d=" "/>', "<defs><linearGradient id='a'/></defs>"])
def test_empty_geometry(body: str):
    assert "empty-geometry" in codes(validate_svg_bytes(svg(body)))


@pytest.mark.parametrize(
    "body",
    [
        '<image xlink:href="photo.png" width="1" height="1"/>',
        '<image href="https://example.com/a.png" width="1" height="1"/>',
        '<path d="M0 0L1 1" style="fill:url(other.svg#g)"/>',
        '<style>@import url("http://x/y.css");</style><path d="M0 0L1 1"/>',
        '<use xlink:href="file:///etc/passwd"/><path d="M0 0L1 1"/>',
    ],
)
def test_external_references_are_errors(body: str):
    report = validate_svg_bytes(svg(body))
    assert not report.valid and "external-reference" in codes(report)


def test_internal_references_resolve_and_dangling_ones_fail():
    good = svg('<defs><path id="g" d="M0 0L1 1"/></defs><use xlink:href="#g"/><path d="M0 0L1 1" fill="url(#g)"/>')
    bad = svg('<path d="M0 0L1 1"/><use xlink:href="#nowhere"/>')
    assert validate_svg_bytes(good).valid
    assert "unresolved-reference" in codes(validate_svg_bytes(bad))


def test_embedded_raster_is_detected_for_vector_output():
    body = '<image href="data:image/png;base64,AAAA" width="1" height="1"/><path d="M0 0L1 1"/>'
    report = validate_svg_bytes(svg(body))
    assert report.valid and "embedded-raster" in codes(report) and report.metrics.images == 1
    assert "image/png" in str(report.issues)
    assert "embedded-raster" not in codes(validate_svg_bytes(svg(body), expect_vector=False))


def test_replacement_character_and_invalid_utf8_are_flagged():
    suspicious = validate_svg_bytes(svg("<path d='M0 0L1 1'/><desc>caf\ufffd</desc>"))
    assert suspicious.valid and "replacement-characters" in codes(suspicious)
    broken = validate_svg_bytes(svg().replace(b"</svg>", b"\xff</svg>"))
    assert "invalid-encoding" in codes(broken) and not broken.valid


def test_entity_declarations_are_refused():
    payload = b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg"/>'
    assert codes(validate_svg_bytes(payload)) == {"unsafe-entity"}


def test_missing_namespace_is_a_warning():
    report = validate_svg_bytes(b'<svg width="1" height="1" viewBox="0 0 1 1"><path d="M0 0L1 1"/></svg>')
    assert report.valid and "missing-namespace" in codes(report)


def test_file_validation_and_unreadable_file(tmp_path: Path):
    path = tmp_path / "a.svg"
    path.write_bytes(GOOD_SVG.encode())
    assert validate_svg_file(path).valid
    assert codes(validate_svg_file(tmp_path / "missing.svg")) == {"unreadable"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [("10", (True, 10.0)), ("1in", (True, 96.0)), ("72pt", (True, 96.0)), ("50%", (True, None)), ("x", (False, None)), ("-1", (False, None))],
)
def test_parse_length(value: str, expected):
    assert parse_length(value) == expected
