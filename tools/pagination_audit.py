"""Audit the pagination OOXML of a DOCX: sections, page-number types, PAGE fields, headers/footers, settings.

Usage: python pagination_audit.py <file.docx> [out.json]
"""

import json
import re
import sys
import zipfile

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"


def q(tag: str) -> str:
    return f"{{{W}}}{tag}"


def text_of(el) -> str:
    return "".join(t.text or "" for t in el.iter(q("t")))


zf = zipfile.ZipFile(sys.argv[1])
names = zf.namelist()
doc = etree.fromstring(zf.read("word/document.xml"))
body = doc.find(q("body"))
rels = etree.fromstring(zf.read("word/_rels/document.xml.rels"))
rel_targets = {r.get("Id"): (r.get("Type").rsplit("/", 1)[-1], r.get("Target")) for r in rels.iter(f"{{{PR}}}Relationship")}

sections = []
paragraphs = list(body.iter(q("p")))
for idx, sect in enumerate(body.iter(q("sectPr"))):
    owner = sect.getparent()
    # first heading/text after the previous section start, for orientation
    pg = sect.find(q("pgNumType"))
    refs = [
        {"kind": child.tag.split("}")[1], "type": child.get(q("type")), "target": rel_targets.get(child.get(f"{{{R}}}id"), (None, None))[1]}
        for child in sect if child.tag in (q("headerReference"), q("footerReference"))
    ]
    sections.append({
        "index": idx,
        "in_paragraph": owner.tag == q("pPr"),
        "type": (sect.find(q("type")).get(q("val")) if sect.find(q("type")) is not None else None),
        "pgNumType": dict((k.split("}")[1], v) for k, v in pg.attrib.items()) if pg is not None else None,
        "titlePg": sect.find(q("titlePg")) is not None,
        "header_footer_refs": refs,
    })

instr = [(t.text or "").strip() for t in doc.iter(q("instrText"))]
simple = [f.get(q("instr")) for f in doc.iter(q("fldSimple"))]
dirty = [f.get(q("dirty")) for f in doc.iter(q("fldChar")) if f.get(q("fldCharType")) == "begin"]
hf_fields = {}
for name in names:
    if re.match(r"word/(header|footer)\d*\.xml$", name):
        part = etree.fromstring(zf.read(name))
        hf_fields[name] = {
            "instr": [(t.text or "").strip() for t in part.iter(q("instrText"))] + [f.get(q("instr")) for f in part.iter(q("fldSimple"))],
            "text": text_of(part)[:80],
        }
settings = zf.read("word/settings.xml").decode("utf-8") if "word/settings.xml" in names else ""
upd = re.search(r"<w:updateFields[^>]*/>", settings)
result = {
    "file": sys.argv[1],
    "sections": sections,
    "section_count": len(sections),
    "page_fields_in_body": sum(1 for i in instr + simple if i and i.split()[:1] == ["PAGE"]),
    "toc_fields": [i for i in instr + simple if i and i.startswith("TOC")],
    "field_begin_dirty_values": sorted(set(str(d) for d in dirty)),
    "header_footer_parts": hf_fields,
    "settings_updateFields": upd.group(0) if upd else None,
    "settings_has_compat_doNotExpandShiftReturn": "doNotExpandShiftReturn" in settings,
    "paragraphs": len(paragraphs),
}
out = json.dumps(result, ensure_ascii=False, indent=1)
if len(sys.argv) > 2:
    open(sys.argv[2], "w", encoding="utf-8").write(out)
print(out)
