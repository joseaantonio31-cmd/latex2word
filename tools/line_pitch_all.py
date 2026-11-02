"""All text lines of one PDF page with their baseline (yMax), height and first words: to read line pitches.

Usage: python line_pitch_all.py <file.pdf> <page>
Needs Poppler's ``pdftotext`` (with ``-bbox-layout``) on PATH, or its path in the PDFTOTEXT environment variable.
"""

import os
import re
import shutil
import subprocess
import sys

POPPLER_PDFTOTEXT = os.environ.get("PDFTOTEXT") or shutil.which("pdftotext") or "pdftotext"
pdf, page = sys.argv[1], sys.argv[2]
xml = subprocess.run([POPPLER_PDFTOTEXT, "-bbox-layout", "-f", page, "-l", page, pdf, "-"],
                     capture_output=True, text=True, encoding="utf-8").stdout
previous = None
for m in re.finditer(r'<line xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</line>', xml, re.S):
    x1, y1, x2, y2 = map(float, m.groups()[:4])
    words = " ".join(re.findall(r">([^<]+)</word>", m.group(5)))[:50]
    delta = "" if previous is None else f"{y2 - previous:+.1f}"
    print(f"x={x1:6.1f} base={y2:6.1f} h={y2 - y1:4.1f} d={delta:>6}  {words}")
    previous = y2
