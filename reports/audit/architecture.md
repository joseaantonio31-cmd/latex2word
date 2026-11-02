# Architecture and risk report

Audit date 2026-11-02. Start commit `35547d4c516c7bcfd65530686bd46b18e703c5ea` (branch `claude/eager-archimedes-0ka8gx`, identical
to `origin/main`, clean tree). Single commit in history: "initial latex2word private-preview baseline".
Size: about 18.7 k lines (Python 16.9 k, Lua 1.6 k), 66 test modules.

Method: static reading of every module named below, plus execution (pytest, real Pandoc 3.1.3 runs, hand-made
fixtures). Where a statement comes from reading only, it says **static**.

## 1. Entry points and execution flow

| Entry point | File | What it does |
|---|---|---|
| `latex2word` console script, `python -m latex2word` | `cli.py`, `__main__.py` | sub-commands `doctor`, `config validate`, `inspect`, `figures`, `build` |
| `run_build()` | `build.py` | the whole conversion pipeline (below) |
| `inspect_toolchain()` | `toolchain.py` | `doctor`: tool discovery + Pandoc capability probe |

Exit codes: 0 OK, 1 document problems, 2 configuration/profile/tool error (`errors.py`).

## 2. Pipeline (data flow) and failure boundaries

```
project.yaml ──config.py (jsonschema)──► ProjectConfig      profile YAML ──profiles.py──► Profile (numbering, layout)
        │                                                                 │
        ▼                                                                 ▼
 [B0] locate_pandoc + (since F-05) assess_pandoc  ── UNSUPPORTED → BuildError (exit 2)
        │
 [B1] source_graph.py + latex_scan.py   comment/verbatim-masked scan of \input/\include/\includegraphics/
        │                                bib/pgfplots data; containment checks (outside-root, symlinks)
        │                                ── any ERROR finding → build stops BEFORE Pandoc runs
 [B2] staging.py + workspace.py         hash-verified copy of reachable files into an owned workspace
 [B3] figure_pipeline.py, tikz*.py,     TikZ → latex (-no-shell-escape) → DVI → dvisvgm → SVG; PDF → pdftocairo/
      pdf_svg.py, svg_validate.py        Inkscape/dvisvgm → SVG; every SVG validated (stdlib expat, entity refusal)
 [B4] rewrite.py + *_rewrite.py,        non-overlapping edits of the STAGED copy, logged to transformations.json
      columntypes.py, siunitx.py,
      bibliography.py, metadata/
 [B5] texprobe.py                       Pandoc +raw_tex probe for unknown commands (would be dropped silently)
 [B6] reference_doc.py + pandoc_adapter.py + filters/*.lua
                                         Pandoc LaTeX reader → AST → Lua filters (numbering, labels/refs, math
                                         normalisation, markers → fields/breaks) → citeproc → DOCX writer
 [B7] docx_validate.py                  structural validation of preliminary.docx
 [B8] ooxml/finalize.py (+ styles, sections, pagination, fields, frontmatter, drawings, tables, equations,
      bibliography, notes)              single lxml pass over the package
 [B9] docx_validate + ooxml/validation  validation of final.docx; on success atomic copy to the output path
```

Failure boundaries: every stage returns findings; any ERROR (or WARNING with `validation.fail_on: warning`)
prevents the output from being written (`build._finish`). Workspaces are kept for diagnosis.

## 3. External dependencies

| Dependency | Role | Present in audit container |
|---|---|---|
| Pandoc (certified 3.12) | reader, Lua runtime, citeproc, DOCX writer | **3.1.3 only** (3.12 install denied by session policy) |
| TeX `latex`, `dvisvgm` | TikZ/pgfplots figures | **absent** → figure pipeline BLOCKED |
| `pdftocairo` / Inkscape / `rsvg-convert` | PDF→SVG, previews | pdftocairo 24.02 only |
| Python: PyYAML, jsonschema, lxml | config, OOXML | present (versions in `logs/environment.txt`) |
| Microsoft Word | final renderer, field update | **absent** |

## 4. Mechanism inventory

* **LaTeX parsing**: no TeX engine. latex2word scans with a masking lexer (`latex_scan.py`) only for
  dependencies, labels and rewrite targets; *semantic* parsing is Pandoc's reader. Two parsers therefore see the
  same text and can disagree (root cause class of F-02 and F-04).
* **Math**: Pandoc keeps TeX in `Math` nodes; `filters/l2w_math.lua` rewrites alignment environments into
  `array` (to avoid texmath's literal `&` in `m:eqArr`), strips `\label`/`\nonumber`; texmath writes OMML.
  Numbered equations become a borderless table/tab layout in `ooxml/equations.py`.
* **Images**: SVG embedded by Pandoc with PNG fallback (needs `rsvg-convert` on older Pandoc); sizes rewritten in
  `ooxml/drawings.py`.
* **Bibliography**: citeproc + CSL. No CSL file ships; without one Pandoc uses its default (Chicago author-date).
  `thebibliography` is rewritten before Pandoc.
* **Word fields**: TOC/LOF/LOT/TC fields are generated (`ooxml/fields.py`, filter markers). Figure/table/equation
  numbers and cross-reference texts are **literal text with hyperlinks**, not SEQ/REF fields (documented).
* **Temp files**: workspace with ownership marker (`workspace.py`), `rmtree` only after marker/link/root/home checks.
* **Logging**: `logging_utils.log_event`, JSON run records (`pandoc_run.json`, `figures_run.json`, manifest).
* **Platform assumptions**: developed on Windows 11; Linux is "design target". Case-insensitive FS handling and
  Windows path tests exist; 3 of them are not portable to Linux (F-09).

## 5. Initial risk ranking (before testing)

| Rank | Component | Why |
|---|---|---|
| 1 | `filters/l2w_math.lua` | rewrites mathematics textually before conversion: any mistake silently alters equations |
| 2 | scanner vs Pandoc reader agreement (`source_graph.py`) | containment checks are only as good as the scanner's model of what Pandoc will read |
| 3 | Pandoc-version coupling (`pandoc_compat.py`, filters) | the filters rely on AST facts pinned for 3.12 |
| 4 | `filters/l2w_semantic.lua` references/numbering | numbering is literal text: a wrong number is not repaired by Word |
| 5 | `ooxml/*` finaliser | direct XML surgery; schema order, relationships, bookmarks |
| 6 | figure pipeline | external TeX execution on untrusted input |

These ranks guided the audit; the results are in `findings.md`.
