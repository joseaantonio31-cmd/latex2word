"""The independent OOXML oracle (tools/audit/ooxml_oracle.py) must detect injected package faults.

A checker that never fails proves nothing; every fault class it claims to detect is injected here into a
minimal hand-written package (no Pandoc, no latex2word code involved).
"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ooxml_oracle", Path(__file__).resolve().parents[1] / "tools" / "audit" / "ooxml_oracle.py"
)
oracle = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(oracle)

CT = (
    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)
ROOT_RELS = (
    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>'
)
DOC_RELS = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
CLEAN_BODY = (
    '<w:p><w:bookmarkStart w:id="0" w:name="sec"/><w:r><w:t>Head</w:t></w:r><w:bookmarkEnd w:id="0"/></w:p>'
    '<w:p><w:hyperlink w:anchor="sec"><w:r><w:t>1</w:t></w:r></w:hyperlink></w:p>'
    '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> TOC \\o "1-3" </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    '<w:p><m:oMath><m:r><m:t>x=1</m:t></m:r></m:oMath></w:p>'
)


def _docx(tmp_path: Path, body: str, *, extra: dict[str, str] | None = None, doc_rels: str = DOC_RELS) -> Path:
    path = tmp_path / "t.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CT)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        zf.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {NS}><w:body>{body}</w:body></w:document>')
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path


def test_clean_package_passes(tmp_path: Path):
    result = oracle.check(_docx(tmp_path, CLEAN_BODY))
    assert result["ok"], result["errors"]
    assert result["facts"]["field_instructions"] == {"TOC": 1} and result["facts"]["omml_equations"] == 1


@pytest.mark.parametrize(
    ("fault", "body", "needle"),
    [
        ("dangling-anchor", CLEAN_BODY.replace('w:anchor="sec"', 'w:anchor="nowhere"'), "anchors without bookmark"),
        ("unpaired-bookmark", CLEAN_BODY.replace('<w:bookmarkEnd w:id="0"/>', ""), "unpaired bookmark"),
        ("duplicate-bookmark", CLEAN_BODY + '<w:p><w:bookmarkStart w:id="9" w:name="sec"/><w:bookmarkEnd w:id="9"/></w:p>',
         "duplicate bookmark names"),
        ("unbalanced-field", CLEAN_BODY.replace('<w:r><w:fldChar w:fldCharType="end"/></w:r>', ""), "unbalanced complex fields"),
        ("tex-remnant", CLEAN_BODY.replace("<m:t>x=1</m:t>", "<m:t>\\frac{a}{b}</m:t>"), "TeX commands left"),
        ("eqarr-ampersand", CLEAN_BODY + "<w:p><m:oMath><m:eqArr><m:e><m:r><m:t>&amp;=</m:t></m:r></m:e></m:eqArr></m:oMath></w:p>",
         "inside m:eqArr"),
        ("undefined-rid", CLEAN_BODY + '<w:p><w:hyperlink r:id="rId77"/></w:p>', "undefined relationship ids"),
    ],
)
def test_injected_faults_are_detected(tmp_path: Path, fault: str, body: str, needle: str):
    result = oracle.check(_docx(tmp_path, body))
    assert not result["ok"] and any(needle in e for e in result["errors"]), (fault, result["errors"])


def test_missing_relationship_target_and_content_type_are_detected(tmp_path: Path):
    rels = DOC_RELS.replace(
        "/>",
        '><Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/gone.png"/></Relationships>',
    )
    result = oracle.check(_docx(tmp_path, CLEAN_BODY, extra={"word/media/x.bin": "data"}, doc_rels=rels))
    assert any("missing part word/media/gone.png" in e for e in result["errors"])
    assert any("part without content type: word/media/x.bin" in e for e in result["errors"])
