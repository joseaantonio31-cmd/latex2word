"""Which paragraphs differ (XML) between two DOCX files with the same paragraph sequence.

Usage: python paragraph_diff.py <a.docx> <b.docx>

Useful to prove that a change touched only the paragraphs it should: prints the number of changed paragraphs per
(style in a, style in b, paragraph properties changed) and whether all paragraph texts are identical. Bookmarks are
ignored in the comparison.
"""
import collections
import re
import sys
import zipfile


def paragraphs(path):
    d = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
    body = d[d.index("<w:body>"):]
    return re.findall(r"<w:p\b(?:(?!<w:p\b).)*?</w:p>", body, re.S)


def style(p):
    m = re.search(r'<w:pStyle w:val="([^"]+)"', p)
    return m.group(1) if m else "-"


def text(p):
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))


def properties(p):
    m = re.search(r"<w:pPr>.*?</w:pPr>", p, re.S)
    return re.sub(r"<w:rPr>.*?</w:rPr>", "", m.group(0)) if m else ""


def without_bookmarks(p):
    return re.sub(r"<w:bookmark(?:Start|End)\b[^>]*/>", "", p)


a, b = paragraphs(sys.argv[1]), paragraphs(sys.argv[2])
if len(a) != len(b):
    sys.exit(f"different paragraph counts: {len(a)} vs {len(b)}")
groups = collections.Counter()
for x, y in zip(a, b):
    if without_bookmarks(x) != without_bookmarks(y):
        groups[(style(x), style(y), properties(x) != properties(y))] += 1
for (sa, sb, changed), count in sorted(groups.items()):
    print(f"{count:6}  {sa} -> {sb}  {'pPr changed' if changed else 'runs only'}")
print("changed paragraphs:", sum(groups.values()), "of", len(a))
print("all paragraph texts identical:", [text(p) for p in a] == [text(p) for p in b])
