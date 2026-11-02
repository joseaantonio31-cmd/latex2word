# latex2word

Convert structured LaTeX projects into editable Microsoft Word documents while preserving equations, tables,
figures, references and document structure.

> **Status: experimental / private preview.** latex2word is under active development. It is not production-ready,
> its configuration format may still change, and no licence has been chosen yet: all rights reserved by the
> owner until a licence is added. Output must be reviewed in Microsoft Word.

## Why

Journals, institutions and collaborators often require `.docx`. Generic converters flatten LaTeX: equations become
images, tables lose their structure, numbering and cross-references turn into stale text, and page layout is lost.
latex2word treats the LaTeX project as the source of truth and produces a Word document that people can keep
editing: native Word equations, real Word tables, vector figures, working internal links and Word-native contents
lists.

It is a careful pipeline around Pandoc, not a TeX engine. Constructs it cannot convert faithfully are reported as
diagnostics, never silently dropped. It has been validated end to end on a 160+ page engineering report with
hundreds of equations, dozens of figures and tables, and an academic front matter, using Microsoft Word as the
renderer.

## Features

- **Equations**: native, editable Word equations (OMML), inline and display; numbered equations laid out with the
  number at the margin; `\eqref`.
- **Tables**: real Word tables from `tabular`, `tabularx`, `longtable` (repeated header rows, final notes),
  `\multicolumn`, booktabs rules, custom column types from `\newcolumntype`; tables taller than a page can break.
- **Figures**: TikZ, circuitikz and pgfplots (including external data files) rendered to vector graphics; PDF and
  SVG graphics converted to vector images; widths resolved against the page or column.
- **Units**: siunitx (`\SI`, `\si`, `\num`, `\SIrange`, `\qty`, `\sisetup`, `\DeclareSIUnit`) converted to text
  and math.
- **Cross-references and numbering**: `\ref`, `\eqref`, `\autoref`, `\cref`/`\Cref` with profile-controlled
  numbering formats and working internal hyperlinks; appendices.
- **Bibliography**: BibTeX/biblatex sources rendered through Pandoc citeproc with a CSL style; inline
  `thebibliography`; citation keys verified before conversion.
- **Contents lists**: Word TOC, list of tables, list of figures and tocloft custom lists (`\newlistof`); manual
  `\addcontentsline` entries; starred headings kept out of the TOC; profile-controlled entry styles.
- **Academic pagination**: `\pagenumbering{gobble|roman|arabic}` becomes Word sections with restarted page
  numbers; blank and title pages; `\clearpage`.
- **Front matter**: title pages with relative font sizes, `\vspace`, `\hrulefill` signature lines; `\title`/
  `\author`/abstract/keywords through profile metadata adapters.
- **Figure and table notes**: note blocks attached to floats keep their source formatting as a Word style.
- **Profiles**: page geometry, columns, typography and numbering live in YAML profiles (`generic`,
  `ieee-conference`, or your own), never in the engine.
- **Safety**: the LaTeX project is never modified; all work happens in an owned, staged copy; output is replaced
  atomically only after validation.

## Requirements

- Python 3.11 or later (developed with 3.14) and the Python packages `PyYAML`, `jsonschema`, `lxml`.
- External tools (found on `PATH`, never installed by latex2word): Pandoc 3.x (3.12 certified, 3.13 tested), a TeX
  distribution (`latex`) and `dvisvgm`. Optional: `pdftocairo`,
  Inkscape, `rsvg-convert`.
- Microsoft Word to view and review the result (Word is the authoritative renderer).

Details: [docs/dependencies.md](docs/dependencies.md). Platforms: developed and validated on Windows 11; Linux/WSL
is a design target but not regularly validated.

## Installation

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
python -m pip install -e ".[dev]"     # or without -e and [dev] for a plain install
latex2word doctor                     # checks Pandoc, TeX and the figure tools
```

## Usage

A project is described by a small YAML file:

```yaml
schema_version: 1
project: {name: my-report, root: ., main: main.tex}
profile: generic                 # or ieee-conference, or a custom profile
profile_dirs: [profiles]         # optional: directories with custom profiles
output: {directory: output}
```

```bash
latex2word config validate project.yaml   # check the configuration
latex2word inspect project.yaml           # read-only: dependencies, bibliography, figures
latex2word figures project.yaml           # render TikZ/PDF figures to SVG (no conversion)
latex2word build project.yaml             # convert to DOCX; writes output/main.docx
latex2word build project.yaml --json      # machine-readable result
```

Exit codes: `0` success, `1` document problems (see the diagnostics), `2` configuration, profile or tool error.
Each build writes a workspace with the staged copy, the transformation log, a build manifest, a layout report and
a `visual_review.md` checklist for the Word review.

## Documentation

| Document | Content |
|---|---|
| [docs/architecture.md](docs/architecture.md) | pipeline and modules |
| [docs/supported-latex.md](docs/supported-latex.md) | supported constructs and caveats |
| [docs/known-limitations.md](docs/known-limitations.md) | known limitations |
| [docs/dependencies.md](docs/dependencies.md) | required, optional and development dependencies |
| [docs/development.md](docs/development.md) | environment, tests, fixtures, workflow |
| [docs/m4-layout.md](docs/m4-layout.md), [docs/equation-layout.md](docs/equation-layout.md), [docs/pandoc-ast-findings.md](docs/pandoc-ast-findings.md) | design notes |
| [tools/README.md](tools/README.md) | manual verification helpers (Word rendering, PDF line metrics) |

## Known limitations (summary)

- Bibliography formatting follows the CSL style, which can differ from a biblatex style.
- Figure, table and equation numbers and cross-reference texts are literal text with working hyperlinks, not Word
  `SEQ`/`REF` fields.
- Very wide equations can render differently because Word's math metrics differ from TeX's.
- Not every macro or package can be reproduced semantically; unknown constructs that would lose text are errors.
- Microsoft Word is the authoritative renderer; final layout must be reviewed there.

See [docs/known-limitations.md](docs/known-limitations.md).

## Tests

```bash
python -m pytest                    # unit and synthetic end-to-end tests (Pandoc tests skip without Pandoc)
python -m pytest -m integration     # optional: real toolchain and external reference projects (env-configured)
```

All selected tests pass; the integration tests are deselected by default and need an external project and a local
expectations file (see [docs/development.md](docs/development.md)).
