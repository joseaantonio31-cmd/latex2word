# Development

## Environment

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
python -m pip install -e ".[dev]"
latex2word doctor
```

The package lives in `src/latex2word/` (src layout); package data (schemas, profiles, Lua filters) is declared in
`pyproject.toml` and ships with a regular install.

## Tests

```bash
python -m pytest                      # default: everything except integration tests
python -m pytest tests/test_note_fidelity.py -q
python -m pytest -m integration       # real toolchain + external reference projects
```

- Unit tests need no external tools. End-to-end tests that need Pandoc skip without it; none needs a TeX
  distribution unless marked `integration`.
- **Synthetic fixtures only**: `tests/fixtures/` holds small, invented LaTeX projects (English and Spanish) and
  test profiles; many tests build their documents in a temporary directory. Never add real documents, private
  text or generated output from real projects.
- `LATEX2WORD_PRIVATE_MARKERS="a,b"` (comma separated) adds locally known private strings (user names, private
  project names) to the tests that check that production code contains none of them.

### Baseline

`python -m pytest` on Windows 11 with Pandoc 3.13: **all selected tests pass** (1,129 passed, 2 skipped, 24
deselected). The two skips are environment-specific (`test_platform_paths.py`: a case-sensitive file system, and the
right to create file symlinks, neither available on a default Windows setup); the deselected tests are the
integration tests below.

### Pandoc versions

- **Certified: 3.12** (`pandoc_compat.CERTIFIED_VERSIONS`). The AST facts the filters rely on are pinned by
  `tests/test_pandoc_ast.py`; certifying another version means re-running that procedure, not editing the set.
- **Tested but uncertified: 3.13.** Any version at or above the 3.0 minimum that is not certified is
  `TESTED_UNCERTIFIED`: `doctor` stays READY and reports warning `L2W-W-PANDOC-003`. Older versions, or a Pandoc that
  fails the capability probe, are `UNSUPPORTED` (`L2W-E-PANDOC-003`; `doctor` is NOT_READY).
- `tests/test_pandoc_compat.py` checks these classes with synthetic version strings; tests that run the real Pandoc
  assert the version that actually ran, never a fixed one.

### Integration tests (`-m integration`)

They run latex2word on an external LaTeX project and skip, with the reason, unless their inputs are provided:

| Variable | Meaning |
|---|---|
| `LATEX2WORD_INTEGRATION_PAPER_ROOT` | root of the external LaTeX project (read-only; must stay byte-identical) |
| `LATEX2WORD_INTEGRATION_MAIN`, `LATEX2WORD_INTEGRATION_MAIN_INLINE` | main files (with a `.bib`; with an inline `thebibliography`) |
| `LATEX2WORD_INTEGRATION_CSL` | CSL style for the project |
| `LATEX2WORD_BASELINE_ROOT` | an earlier reference conversion to compare against |
| `LATEX2WORD_INTEGRATION_EXPECTATIONS` | **local JSON file with the project-specific expectations** |
| `LATEX2WORD_INTEGRATION_WORKSPACE`, `..._M3_WORKSPACE`, `..._M4_WORKSPACE` | optional kept workspaces |

The tracked tests contain no facts about the external project. Its expectations live in the JSON file
(`tests/integration_project.py` loads it): keep it outside the repository; `*.local.json` is ignored if kept inside.
Keys:

```json
{
  "figure_patches": [{"figure": "<id>", "find": "<tikz text>", "replace": "<tikz text>"}],
  "figures": {"active": {"<id>": "tikzpicture|circuitikz|pdf"}, "inactive": ["<id>"],
              "staged_present": ["<path>"], "staged_absent": ["<path>"], "staged_absent_prefixes": ["<dir>/"],
              "width_original": "<width>", "width_emitted": "<width>", "width_fraction": 0.98},
  "numbering": {"figures": ["Fig. 1."], "tables": ["TABLE I"], "equations": ["(1)"],
                "sections": ["I."], "unnumbered_headings": ["<heading>"]},
  "counts": {"active_figure_assets": 0, "bibliography_entries": 0, "math_alignment_environments_normalised": 0,
             "math_display": 0, "math_inline": 0, "authors": 0, "data_tables": 0, "native_matrices": 0},
  "baseline": {"figures": [1], "tables": [1], "equations": [1], "bibliography_paragraphs": 0}
}
```

## Workflow

- Every conversion change gets a synthetic regression test (a minimal fixture under `tests/fixtures/` or a
  generated document in `tmp_path`).
- Engine code must not branch on document classes, publishers or specific documents; behaviour that differs
  between publishers belongs in profiles.
- Verify layout in Microsoft Word (`tools/word_render.ps1`), not only structurally.
- Local runs belong in `runs/` or `work/` (ignored); never commit converted documents.
