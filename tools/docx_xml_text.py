"""Visible text of a DOCX straight from word/document.xml: one line per paragraph (w:t and m:t runs).

Usage: python docx_xml_text.py <file.docx> <out.txt>
"""
import sys
import zipfile

from lxml import etree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
root = etree.fromstring(zipfile.ZipFile(sys.argv[1]).read("word/document.xml"))
lines = []
for p in root.iter(W + "p"):
    if any(a.tag == W + "p" for a in p.iterancestors()):
        continue
    lines.append("".join(t.text or "" for t in p.iter(W + "t", M + "t")))
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(lines))
print(len(lines), "paragraphs")
