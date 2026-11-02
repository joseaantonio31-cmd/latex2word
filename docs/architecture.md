# Architecture

latex2word is a pipeline of small, testable steps around Pandoc. The LaTeX project is a read-only input; every
change is made to a staged copy inside a workspace that latex2word owns, and the requested output is replaced only
after the result has been validated.

```
project.yaml + profile
      │
      ▼
1. analyse        source graph (inputs, packages, local classes, bibliography, figures, pgfplots data)
2. stage          hash-verified copy of the reachable files into the workspace
3. figures        TikZ/circuitikz/pgfplots and PDF graphics → SVG (latex + dvisvgm, PDF backends)
4. rewrite        staged-copy edits with a transformation log (see "Staged rewrites")
5. probe          report constructs Pandoc would silently drop
6. Pandoc         LaTeX → DOCX (+ citeproc) with the latex2word Lua filters and a profile-generated reference.docx
7. finalise       one OOXML pass: styles, sections, pagination, front matter, tables, figures, equations, notes
8. validate       structural and layout checks; atomic replacement of the output
```

## Steps

1. **Analysis** (`source_graph.py`, `latex_scan.py`, `inspection.py`, `labels.py`): comment- and verbatim-aware
   scanning of the main file and everything it inputs; missing or outside-root dependencies are diagnostics.
2. **Staging** (`staging.py`, `workspace.py`): only reachable files are copied; workspaces carry an ownership marker
   and are the only directories latex2word ever deletes.
3. **Figures** (`figure_registry.py`, `figure_pipeline.py`, `tikz.py`, `tikz_render.py`, `pdf_svg.py`,
   `svg_validate.py`, `previews.py`): each active figure is rendered to SVG and validated; a manifest records
   sizes and aspect ratios.
4. **Staged rewrites** (`rewrite.py` applies non-overlapping edits and records each one):
   - `figure_rewrite.py`: figures point at their SVG; width expressions normalised;
   - `columntypes.py`: `\newcolumntype` expansion, `longtable` head/foot handling, `\multicolumn` cleanup;
   - `siunitx.py`: siunitx commands to text and math;
   - `structure_rewrite.py`: page breaks, `\pagenumbering`, TOC/LOT/LOF and custom lists, manual contents
     entries, appendix and bibliography placement, front-matter typesetting (sizes, `\vspace`, `\hrulefill`,
     `\null`) and figure/table note blocks, each as a marker environment the filter understands;
   - `bibliography.py`, `metadata/`: bibliography sources and front-matter metadata.
5. **Probe** (`texprobe.py`): a Pandoc AST pass that finds unknown commands whose text would vanish.
6. **Pandoc** (`pandoc_adapter.py`, `pandoc_compat.py`, `reference_doc.py`, `filters/*.lua`): the Lua filters
   normalise math, number headings/figures/tables/equations from the profile, resolve labels and references,
   turn the markers into Word constructs (fields, page breaks, sections, styles) and report what they did.
7. **Finalisation** (`ooxml/`): a single pass over the DOCX package: styles and typography (`styles.py`), sections
   and page numbering (`sections.py`, `pagination.py`), contents fields (`fields.py`), unlisted headings
   (`headings.py`), front matter (`frontmatter.py`), figures (`drawings.py`), tables (`tables.py`), equations
   (`equations.py`), bibliography (`bibliography.py`), notes (`notes.py`).
8. **Validation** (`docx_validate.py`, `docx_package.py`, `ooxml/validation.py`, `review.py`): package integrity,
   relationships, bookmarks, numbering, math, figures, bibliography, layout invariants; a layout report and a
   checklist for the review in Word. The output file is replaced atomically.

## Profiles and configuration

`config.py` loads and validates the project file (`schemas/project.schema.json`); `profiles.py` loads YAML profiles
(`schemas/profile.schema.json`, `extends` inheritance) into numbering, bibliography and layout settings
(`layout.py`, `units.py`, `numerals.py`). The engine never branches on the document class or publisher: that is
what profiles are for.

## Toolchain and processes

`toolchain.py` discovers and probes external tools (`doctor`); `process.py` and `toolrun.py` run them without a
shell, with timeouts and logged arguments; `diagnostics.py` and `errors.py` define the `L2W-<E|W|I>-<AREA>-<nnn>`
diagnostics and exit codes; `cli.py` is the command surface.

## Design notes

- [m4-layout.md](m4-layout.md): profile-driven layout, front matter, why numbers are literal text.
- [equation-layout.md](equation-layout.md): numbered display equations in Word.
- [pandoc-ast-findings.md](pandoc-ast-findings.md): the Pandoc behaviour the pipeline relies on.
