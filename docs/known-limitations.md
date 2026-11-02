# Known limitations

latex2word is an experimental private preview. The limitations below are known and deliberate or not yet solved.

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
  places or access dates, title capitalisation).
- **Numbers and cross-reference texts are literal text** with working internal hyperlinks, not Word `SEQ`/`REF`
  fields; inserting a figure in Word does not renumber the others.
- **Not every macro can be reproduced semantically.** Macros are expanded by Pandoc; layout-oriented commands are
  honoured only where a rewrite exists (front-matter sizes and spacing, notes, contents, pagination). Unknown
  constructs that would lose text are errors.
- `\nameref`, `\pageref`, `\tag`, `\intertext`, sub-figures and EPS/TIFF graphics are not converted.
- Contents lists use Word's own entry text: list entries show the caption text ("Table 2.1. …") rather than a
  separate number column, and spacing between chapter groups is not reproduced.
- Spacing that LaTeX adds around floats and long tables (`\textfloatsep`, `\LTpost`) is not reproduced exactly.

## Toolchain

- Pandoc 3.12 is the certified version; 3.13 is tested and works but reports `TESTED_UNCERTIFIED`.
- Validated mainly on Windows 11 with MiKTeX; Linux/WSL is a design target without regular validation.
