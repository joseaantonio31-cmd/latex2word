# Known limitations

latex2word is an experimental private preview. The limitations below are known and deliberate or not yet solved.

## Security

- **latex2word currently assumes trusted LaTeX input. Do not use it to process untrusted or third-party LaTeX
  projects.** Pandoc's LaTeX reader is confined by default and cannot read outside the validated project, but TeX
  may read any readable file (`\input`, `\openin`) while rendering a TikZ, circuitikz or pgfplots figure (audit F-07,
  not yet isolated). Details: [SECURITY.md](../SECURITY.md).
- **The confined reader refuses what it cannot flatten** (`L2W-E-CONFINE-004`) and any read outside the flattened
  source (`L2W-E-CONFINE-001`). `LATEX2WORD_UNSAFE_LEGACY_READER=1` restores the former unconfined reader to diagnose
  such a project; it is unsafe and warned (`L2W-W-CONFINE-005`).

## Rendering

- **Microsoft Word is the authoritative renderer.** latex2word validates the DOCX structurally, but line breaks,
  page breaks and float placement are decided by Word. Every build writes a `visual_review.md` checklist; final
  layout must be reviewed in Word. LibreOffice and DOCX libraries may render differently.
- **Page counts are approximate.** Word and TeX use different line breaking and font metrics; a long document can
  differ by a page or so even when all typography settings match.
- **Very wide equations** can render differently, because Word's math layout and font metrics (Cambria Math or the
  profile font) differ from TeX's; a formula that just fits in LaTeX can be clipped or wrapped in Word.

## Semantics

- **Bibliography formatting follows the CSL style**, which can differ from a biblatex style (fields such as notes,
  places or access dates, title capitalisation). No CSL file ships with latex2word: a declared style such as
  `style=apa` without a configured CSL uses Pandoc's default (Chicago author-date) and is reported as
  `L2W-W-BIB-014`; a CSL of another style as `L2W-W-BIB-015`; a style with no known CSL equivalent as `L2W-W-BIB-016`.
- **Multiple numbered rows in one `align`/`gather`** are an explicit error (`L2W-E-MATH-003`): use `\nonumber` on all
  rows but one, or one environment per numbered equation.
- **Numbers and cross-reference texts are literal text** with working internal hyperlinks, not Word `SEQ`/`REF`
  fields; inserting a figure in Word does not renumber the others.
- **Not every macro can be reproduced semantically.** Macros are expanded by Pandoc; layout-oriented commands are
  honoured only where a rewrite exists (front-matter sizes and spacing, notes, contents, pagination). Unknown
  constructs that would lose text are errors.
- `\nameref`, `\pageref`, `\tag`, `\intertext`, sub-figures and EPS/TIFF graphics are not converted.
- **TikZ figures see only the main file's preamble.** Each drawing is rendered as a standalone document whose
  preamble is derived from the main file: drawing packages, TikZ/pgfplots settings, colours and the macros the drawing
  uses. A macro, colour or style defined only in a local `.sty` (or another file the preamble loads) is not available:
  the figure fails to render (`L2W-E-FIG-001`) instead of being converted wrongly. Define such commands in the main
  preamble. This concerns TikZ, circuitikz and pgfplots drawings only: local `.sty` macros used in the text are
  converted as usual.
- Contents lists use Word's own entry text: list entries show the caption text ("Table 2.1. …") rather than a
  separate number column, and spacing between chapter groups is not reproduced.
- Spacing that LaTeX adds around floats and long tables (`\textfloatsep`, `\LTpost`) is not reproduced exactly.

## Toolchain

- Pandoc 3.12 is the certified version. Pandoc 3.13 passes the full test suite and the private end-to-end validation
  with the same results, but stays `TESTED_UNCERTIFIED` until it is certified deliberately (see
  [development.md](development.md)). Every build probes Pandoc: a reader whose AST differs from the
  pinned facts is refused (`L2W-E-PANDOC-003`); a probe that cannot run is a tool error (`L2W-E-PANDOC-004`), not a
  verdict on the version.
- Validated mainly on Windows 11 with MiKTeX; Linux/WSL is a design target without regular validation.
