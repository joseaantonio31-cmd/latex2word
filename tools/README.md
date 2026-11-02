# Developer tools

Small, standalone helpers for checking converted documents by hand. They are not part of the `latex2word`
package and are not used by the test suite. Microsoft Word is the authoritative DOCX renderer, so final layout checks
render with Word, not with LibreOffice or a DOCX parser.

| Tool | Purpose | Needs |
|---|---|---|
| `word_render.ps1` | Opens a **copy** of a DOCX in Word (hidden), updates fields and TOCs, writes statistics (pages, sections, tables, equations, TOC text) as JSON and exports a PDF. Optionally saves the field-updated copy. | Windows, Microsoft Word |
| `line_pitch_all.py` | Baseline, height and first words of every text line on a PDF page, to compare line pitch and spacing between a LaTeX PDF and a Word PDF. | Poppler `pdftotext` (PATH or `PDFTOTEXT`) |
| `docx_xml_text.py` | Visible text of a DOCX, one line per paragraph, straight from `word/document.xml`. | Python |
| `coverage.py` | Word-level coverage of a reference PDF's text (from `pdftotext`) by a DOCX's text. | Python |
| `pagination_audit.py` | Sections, page-number formats, PAGE fields, headers/footers and settings of a DOCX. | Python, lxml |
| `paragraph_diff.py` | Which paragraphs changed between two DOCX builds with the same paragraph sequence. | Python |

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\word_render.ps1 -Docx out.docx -Pdf out.pdf -Json out.json
pdftotext -enc UTF-8 reference.pdf reference.txt
python tools\docx_xml_text.py out.docx out.txt
python tools\coverage.py reference.txt out.txt
```
