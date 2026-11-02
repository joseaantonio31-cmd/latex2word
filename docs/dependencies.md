# Dependencies

`latex2word doctor` reports what is installed and usable. Versions marked "tested" are the ones the current
preview was validated with (Windows 11).

## Python

| Package | Class | Used for | Tested version |
|---|---|---|---|
| Python | REQUIRED | runtime (>= 3.11) | 3.14.7 |
| `PyYAML` (>= 6.0) | REQUIRED | configuration and profiles | 6.0.3 |
| `jsonschema` (>= 4.18) | REQUIRED | configuration and profile validation | 4.26.0 |
| `lxml` (>= 5.2) | REQUIRED | OOXML editing and validation | 6.1.3 |
| `pytest` (>= 8) | DEVELOPMENT (`.[dev]`) | test suite | 9.1.1 |

## External executables

Found on `PATH`; never installed or downloaded by latex2word.

| Tool | Class | Used for | Tested version |
|---|---|---|---|
| Pandoc | EXTERNAL, REQUIRED | LaTeX reader, DOCX writer, Lua filters, citeproc | 3.13 (certified: 3.12) |
| `latex` | EXTERNAL, REQUIRED | TikZ/circuitikz/pgfplots figures (DVI) | MiKTeX-pdfTeX 4.27 (MiKTeX 26.5) |
| `pdflatex` | EXTERNAL, OPTIONAL | reported as a TeX capability by `doctor` only; no build step runs it | MiKTeX-pdfTeX 4.27 |
| `dvisvgm` | EXTERNAL, REQUIRED | DVI/PDF to SVG | 3.6 |
| `kpsewhich` | EXTERNAL, OPTIONAL | reported as a TeX capability by `doctor` only | MiKTeX 26.5 |
| `pdftocairo` (Poppler) | EXTERNAL, OPTIONAL | preferred PDF-graphic to SVG backend | 24.04.0 |
| Inkscape | EXTERNAL, OPTIONAL | alternative PDF-to-SVG backend | 1.4.4 |
| `rsvg-convert` | EXTERNAL, OPTIONAL | PNG previews of figures (`figures --previews`) | 2.63.0 |
| `pandoc-tex-numbering` | EXTERNAL, OPTIONAL (legacy) | reported by `doctor`; not used by the current pipeline | 1.3.4 |
| Microsoft Word | EXTERNAL, not used by the package | viewing and reviewing the output; `tools/word_render.ps1` | desktop Word for Windows (version not recorded) |
| PowerShell | INFORMATIONAL | `tools/word_render.ps1` only | 5.1 |

PDF graphics need at least one PDF backend (`pdftocairo`, Inkscape or `dvisvgm`, tried in that order). `doctor`
treats `pandoc`, `latex` and `dvisvgm` as required for readiness even when a project has no TikZ or PDF figures;
the conversion itself only invokes the figure tools for such figures. Pandoc 3.12 is certified; other versions from
3.0 on are usable but reported as `TESTED_UNCERTIFIED` (see [development.md](development.md)).
