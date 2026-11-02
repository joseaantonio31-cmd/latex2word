# OOXML validation

## Independent oracle

`tools/audit/ooxml_oracle.py` imports nothing from latex2word. It re-derives from the ZIP:
content-type coverage, internal relationship targets, `r:id`/`r:embed` resolution, bookmark pairing and name
uniqueness, hyperlink anchors, complex-field balance (`w:fldChar` begin/end), field instruction counts, OMML counts,
`&` inside `m:eqArr`, and TeX remnants in math (`\name` in `m:t`, what Pandoc emits when texmath fails).

The oracle is itself tested: `tests/test_audit_oracle.py` injects each fault class into a hand-written package
(dangling anchor, unpaired/duplicate bookmark, unbalanced field, TeX remnant, `&` in `eqArr`, undefined `r:id`,
missing relationship target, part without content type) and checks that it is reported. **9/9 pass.**

## Results

| Document | How produced | Oracle | Notes |
|---|---|---|---|
| `tests/fixtures/audit/clean_article` → `main.docx` | harness build (schema-order check and Pandoc gate bypassed, Pandoc 3.1.3) | **OK**, 0 errors | 16 parts, 3 bookmarks, 1 internal hyperlink, `TOC` field ×1, 10 `m:oMath` (4 display), 1 table, 0 TeX remnants |
| same, second build in another workspace | harness | byte-identical (SHA-256 `e6fa760b…d338d2`) | determinism |

OMML structure of the clean fixture (checked against the source, `test_audit_e2e.py`): 3 `m:f` (a/b, binomial,
sin x/x), 1 `m:rad` with `m:deg` = 3, `m:nary` with ∫ and ∑, 1 `m:limLow`, 3 matrices, **0 `m:eqArr`**, no `&` text,
row `[x,y]` present as an `m:d` with `begChr="["`.

## What could not be verified

| Property | Status | Why |
|---|---|---|
| Section properties, page numbering restarts, headers/footers on a real thesis | NOT TESTED end to end | covered by existing tests that need Pandoc 3.12 (BLOCKED here) |
| LOF/LOT/TC fields, caption styles | partially (structure only, TOC on clean fixture) | BLOCKED end to end |
| Word opening the file without repair, rendering, field update (F9), equation editability | **NOT TESTED** | Microsoft Word unavailable; schema-valid XML does not prove Word accepts or renders it |
| Validation against the ECMA-376 XSD | NOT TESTED | schemas not available offline; latex2word checks child order for 7 container types only |
| Footnotes/endnotes | NOT TESTED in this audit | existing `test_note_fidelity.py` needs Pandoc 3.12 |

Word-dependent claims: TOC, LOF and LOT fields are written with placeholder text ("Update this field (F9)…").
Their content only appears after Word updates the fields. Nothing in the cloud environment can confirm that.
