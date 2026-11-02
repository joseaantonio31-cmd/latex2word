# Scientific-document fidelity metrics

The metrics are defined so that they cannot be inflated. Each has three separate layers:
**structural** (machine-checkable package/XML facts), **semantic** (the mathematics or reference means the same as
the source), **visual** (what Word renders). A count is reported only where the denominator comes from the source
by an independent method.

## Definitions

| Metric | Definition | Denominator source | Layer | Limitation |
|---|---|---|---|---|
| R_eq,struct | equations present as OMML with no TeX remnant ÷ N_expected | N_expected = math nodes in Pandoc's AST of the **source** (inline + display), counted before filters | structural | uses Pandoc's reader for the count; an equation the reader drops is missed by both sides |
| R_eq,sem | equations whose linearised OMML (with delimiters, n-ary characters, radical degrees) equals a hand-derived expectation ÷ equations with such an expectation | hand-written expectations | semantic | expensive; only small fixtures |
| R_num | equations/figures/tables carrying the source's number ÷ numbered objects in the source | unstarred numbered environments without `\nonumber`/`\notag` | semantic | `align` rows need row-level counting |
| R_ref | references rendered as a resolved hyperlink with the expected number ÷ reference commands in the source | `labels.scan_labels` over the source (each label in a comma list counted once) | semantic | `\pageref`/`\nameref` are errors by design |
| R_fig | figures embedded (`w:drawing` with a valid `r:embed`) ÷ `\includegraphics` + TikZ figures in the source | source graph | structural | vector editability in Word not measurable without Word |
| R_tab | Word tables ÷ `tabular`-family environments in the source | source scan | structural | merged-cell correctness needs per-cell expectations |
| R_bib | bibliography entries ÷ distinct cited keys (+ `\nocite`) | source scan + `.bib` | structural | style conformance (APA) is a separate, manual check |
| R_build | builds with exit 0 and an oracle-clean package ÷ builds attempted | test fixtures | reliability | |
| Word editability | NOT MEASURABLE here | — | visual | needs Word; XML validity does not imply editability |

Rules: a mathematically wrong equation that is still OMML counts toward R_eq,struct but **not** R_eq,sem. A
reference showing `??` counts as unresolved.

## Measured values (audit container, Pandoc 3.1.3)

| Fixture | Metric | Value | Status |
|---|---|---|---|
| `tests/fixtures/audit/clean_article` (harness build) | R_eq,struct | 10/10 OMML, 0 TeX remnants (denominator: 10 math nodes in the fixture source: 5 inline in §1, 4 display, 1 inline in the table cell) | VERIFIED (harness) |
| same | R_eq,sem | 4/4 display equations equal their hand-derived linearisation; element counts match (see `ooxml-validation.md`) | VERIFIED (harness) |
| same | R_ref | 1/1 (`\ref{sec:math}` → hyperlink, bookmark present) | VERIFIED (harness) |
| same | R_tab | 1/1 | VERIFIED (harness) |
| same | R_build (repeat) | 2/2, byte-identical | VERIFIED (harness) |
| `report_structure` fixture, real build | R_ref before F-04 | 7 resolved of 13 source references; 4 unresolved shown as `??` (3 equation labels + `tab:ancha`, Pandoc 3.1.3 AST differences, all reported as errors), **2 silently absent** (`\Cref`) | VERIFIED — the 2 silent losses are now error F-04 |
| equation fixture, real build before F-05 | R_num | **0/3** with no diagnostic | VERIFIED — now refused by F-05 |
| historical thesis | all | — | NOT TESTED (thesis not available, F-10) |

The 1 414 / 54 / 21 / 31 / 74 figures from the mission statement are **not** used as denominators: they cannot be
re-derived from the repository.
