"""Word-coverage comparison between the authoritative PDF text and the DOCX text.

Usage: python coverage.py <pdf.txt> <docx.txt> [report.json]

Tokens are lowercase words of >= 4 letters. Reports, per PDF word, how many
PDF occurrences have no DOCX counterpart (multiset difference), the overall
coverage, and the most frequent missing words with a PDF context line. The PDF
side contains running heads, page numbers, the TOC/lists and hyphenation
splits, so 100 % is not expected; large clusters of missing words point at
lost content.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("­", "")
    text = re.sub(r"-\n\s*", "", text)  # PDF end-of-line hyphenation
    return text.lower()


def main() -> int:
    pdf = norm(Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace"))
    docx = norm(Path(sys.argv[2]).read_text(encoding="utf-8", errors="replace"))
    pw, dw = Counter(WORD.findall(pdf)), Counter(WORD.findall(docx))
    missing = pw - dw
    extra = dw - pw
    total = sum(pw.values())
    lost = sum(missing.values())
    lines = pdf.splitlines()
    top = []
    for word, count in missing.most_common(60):
        context = next((ln.strip() for ln in lines if word in ln), "")
        top.append({"word": word, "missing": count, "pdf": pw[word], "docx": dw[word], "context": context[:110]})
    result = {
        "pdf_words": total,
        "docx_words": sum(dw.values()),
        "missing_occurrences": lost,
        "coverage": round(1 - lost / total, 4) if total else None,
        "top_missing": top,
        "top_extra": [{"word": w, "extra": c} for w, c in extra.most_common(25)],
    }
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if len(sys.argv) > 3:
        Path(sys.argv[3]).write_text(text, encoding="utf-8")
    print(f"PDF words {total}, DOCX words {result['docx_words']}, missing {lost}, coverage {result['coverage']}")
    for item in top[:40]:
        print(f"  {item['word']:<22} missing {item['missing']:>4} (pdf {item['pdf']}, docx {item['docx']})  | {item['context']}")
    print("extra in DOCX:", ", ".join(f"{e['word']}+{e['extra']}" for e in result["top_extra"][:20]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
