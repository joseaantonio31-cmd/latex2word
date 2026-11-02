"""Independent OOXML integrity oracle for audit use.

Deliberately imports nothing from ``latex2word``: it re-derives package facts from the
ZIP and the XML so that a defect in latex2word's own validators cannot hide a defect
in its output. Checks:

* ZIP: duplicate entries, absolute or ``..`` entry names;
* ``[Content_Types].xml`` covers every part (Override or Default by extension);
* every internal relationship target of every ``.rels`` part exists;
* every ``r:id`` / ``r:embed`` / ``r:link`` in ``word/document.xml`` resolves in its rels;
* bookmarks: ``w:bookmarkStart``/``w:bookmarkEnd`` ids pair up, names unique;
* every ``w:hyperlink/@w:anchor`` names an existing bookmark;
* complex fields: ``w:fldChar`` begin/separate/end are balanced (document order);
* field instructions (``TOC``, ``SEQ``, ``REF``, ``PAGE`` ...) are counted;
* OMML: number of ``m:oMath``, ``&`` inside ``m:eqArr``, and math text that still looks
  like TeX (a backslash followed by letters), which is what Pandoc emits when texmath
  fails ("rendering as TeX").

Usage: ``python tools/audit/ooxml_oracle.py file.docx [--json]``. Exit code 1 if any
integrity error is found.
"""

from __future__ import annotations

import json
import posixpath
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
_TEX_REMNANT = re.compile(r"\\[A-Za-z]{2,}")


def _xml(zf: zipfile.ZipFile, name: str) -> etree._Element:
    return etree.fromstring(zf.read(name), _PARSER)


def _rels_name(part: str) -> str:
    head, tail = posixpath.split(part)
    return posixpath.join(head, "_rels", tail + ".rels")


def check(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    facts: dict[str, Any] = {}
    with zipfile.ZipFile(path) as zf:
        names = [i.filename for i in zf.infolist()]
        dup = sorted(n for n, c in Counter(names).items() if c > 1)
        if dup:
            errors.append(f"duplicate ZIP entries: {dup}")
        unsafe = [n for n in names if n.startswith("/") or ".." in n.split("/") or "\\" in n]
        if unsafe:
            errors.append(f"unsafe ZIP entry names: {unsafe}")
        parts = [n for n in names if not n.endswith("/")]
        facts["parts"] = len(parts)

        # content types
        ct = _xml(zf, "[Content_Types].xml")
        defaults = {d.get("Extension").lower() for d in ct.iter(f"{{{CT}}}Default")}
        overrides = {o.get("PartName").lstrip("/") for o in ct.iter(f"{{{CT}}}Override")}
        for part in parts:
            if part == "[Content_Types].xml":
                continue
            ext = part.rsplit(".", 1)[-1].lower() if "." in part else ""
            if part not in overrides and ext not in defaults:
                errors.append(f"part without content type: {part}")
        for o in overrides:
            if o not in parts:
                errors.append(f"content-type override for a missing part: {o}")

        # relationships
        rel_ids: dict[str, dict[str, str]] = {}
        for rels in (n for n in parts if n.endswith(".rels")):
            source_dir = posixpath.dirname(posixpath.dirname(rels))
            ids: dict[str, str] = {}
            for rel in _xml(zf, rels).iter(f"{{{PR}}}Relationship"):
                rid, target, mode = rel.get("Id"), rel.get("Target"), rel.get("TargetMode")
                if rid in ids:
                    errors.append(f"{rels}: duplicate relationship id {rid}")
                ids[rid] = target
                if mode == "External":
                    continue
                resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(source_dir, target))
                if resolved not in parts:
                    errors.append(f"{rels}: {rid} -> missing part {resolved}")
            rel_ids[rels] = ids

        doc = _xml(zf, "word/document.xml")
        doc_rels = rel_ids.get(_rels_name("word/document.xml"), {})
        missing_rids = sorted(
            {v for e in doc.iter() for k, v in e.attrib.items() if k in (f"{{{R}}}id", f"{{{R}}}embed", f"{{{R}}}link")}
            - set(doc_rels)
        )
        if missing_rids:
            errors.append(f"document.xml references undefined relationship ids: {missing_rids}")

        # bookmarks and anchors
        starts = {b.get(f"{{{W}}}id"): b.get(f"{{{W}}}name") for b in doc.iter(f"{{{W}}}bookmarkStart")}
        ends = {b.get(f"{{{W}}}id") for b in doc.iter(f"{{{W}}}bookmarkEnd")}
        names_count = Counter(starts.values())
        if set(starts) != ends:
            errors.append(f"unpaired bookmark ids: starts-only={sorted(set(starts) - ends)} ends-only={sorted(ends - set(starts))}")
        dup_names = sorted(n for n, c in names_count.items() if c > 1)
        if dup_names:
            errors.append(f"duplicate bookmark names: {dup_names}")
        anchors = [h.get(f"{{{W}}}anchor") for h in doc.iter(f"{{{W}}}hyperlink") if h.get(f"{{{W}}}anchor")]
        dangling = sorted({a for a in anchors if a not in names_count})
        if dangling:
            errors.append(f"hyperlink anchors without bookmark: {dangling}")
        facts["bookmarks"] = len(starts)
        facts["internal_hyperlinks"] = len(anchors)

        # complex fields
        depth, bad = 0, 0
        for fc in doc.iter(f"{{{W}}}fldChar"):
            kind = fc.get(f"{{{W}}}fldCharType")
            if kind == "begin":
                depth += 1
            elif kind == "end":
                depth -= 1
                if depth < 0:
                    bad += 1
                    depth = 0
        if depth or bad:
            errors.append(f"unbalanced complex fields (open={depth}, stray end={bad})")
        instr = " ".join("".join(t.itertext()) for t in doc.iter(f"{{{W}}}instrText"))
        simple = [f.get(f"{{{W}}}instr") or "" for f in doc.iter(f"{{{W}}}fldSimple")]
        all_instr = re.findall(r"\b(TOC|SEQ|REF|PAGEREF|PAGE|NUMPAGES|HYPERLINK|STYLEREF|TC)\b", instr + " " + " ".join(simple))
        facts["field_instructions"] = dict(Counter(all_instr))

        # math
        omath = list(doc.iter(f"{{{M}}}oMath"))
        facts["omml_equations"] = len(omath)
        facts["omml_display"] = len(list(doc.iter(f"{{{M}}}oMathPara")))
        amp = sum(("&" in (t.text or "")) for arr in doc.iter(f"{{{M}}}eqArr") for t in arr.iter(f"{{{M}}}t"))
        if amp:
            errors.append(f"{amp} '&' run(s) inside m:eqArr (alignment marker leaked)")
        remnants = sorted({m for t in doc.iter(f"{{{M}}}t") for m in _TEX_REMNANT.findall(t.text or "")})
        facts["omml_tex_remnants"] = remnants
        if remnants:
            errors.append(f"TeX commands left as math text (failed conversion): {remnants}")
        # plain-text TeX remnants in ordinary runs (e.g. raw $...$ or \command that Pandoc printed verbatim)
        text_remnants = sorted({m for t in doc.iter(f"{{{W}}}t") for m in _TEX_REMNANT.findall(t.text or "")})
        facts["text_tex_remnants"] = text_remnants

        facts["tables"] = len(list(doc.iter(f"{{{W}}}tbl")))
        facts["drawings"] = len(list(doc.iter(f"{{{W}}}drawing")))
        facts["paragraphs"] = len(list(doc.iter(f"{{{W}}}p")))
        facts["sections"] = len(list(doc.iter(f"{{{W}}}sectPr")))
    return {"file": str(path), "ok": not errors, "errors": errors, "facts": facts}


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    files = [a for a in argv if a != "--json"]
    status = 0
    for name in files:
        result = check(Path(name))
        status |= 0 if result["ok"] else 1
        if as_json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"{name}: {'OK' if result['ok'] else 'ERRORS'}")
            for e in result["errors"]:
                print(f"  ERROR {e}")
            for k, v in result["facts"].items():
                print(f"  {k}: {v}")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
