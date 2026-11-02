# Remediation phase 2: structural hardening before local certification

Branch `claude/eager-archimedes-0ka8gx`. Phase start `9304bd3`; commits `98b8351` … `028ff97` plus this report.
Environment: Pandoc 3.1.3, no TeX, no Microsoft Word (`logs/environment.txt`). **Nothing here verifies Pandoc
3.12/3.13 behaviour, TikZ rendering or Word rendering/editability.**

Branch note: pull request #1 (from this branch) was closed without merging and the remote branch was deleted.
`main` is unchanged at `35547d4`. Work continued on the same branch name as instructed. No PR was opened, nothing
was merged.

## 0. Current state, re-verified before editing

Each claim from the phase-1 reports was re-executed here first (scripts under `tools/audit/phase2`, raw outputs in
the session).

| ID | Status at phase start | Reproducible here? | Reproduced | Action in this phase |
|---|---|---|---|---|
| F-02 residual | `\iffalse` nesting and `filecontents` leak an outside file | yes | canary in `preliminary.docx` **and final DOCX** (harness build) | R3 prototype |
| F-04 | set-based; `\Cref{a}` dropped + `\ref{a}` surviving passes | yes | build OK, text "As shows, see also" | per-occurrence invariant |
| F-05 | probe timeout/launch error → UNSUPPORTED; API pin `(1,23,1)` | yes | `assess_pandoc` with a timing-out runner → `UNSUPPORTED ('latex-reader: timed out')` | PROBE_ERROR path, API rule |
| F-06 | `--output main.tex` overwrites the source | yes (now dynamically) | `main.tex` became a "Microsoft Word 2007+" file after a harness build | output-safety policy |
| F-07 | TeX may read any file during TikZ rendering | no (no TeX) | — | threat-boundary note only |
| F-09 | 3 non-portable tests | yes | 3 failures on Linux | fixed |
| F-11 | `style=apa` silently becomes Pandoc's default CSL | yes | harness build OK, no diagnostic | detection |
| F-12 | multiple numbered rows → error | yes | `L2W-E-MATH-003` for `align` and `gather` | verify only |

## 1. F-06: output safety

**Policy** (`src/latex2word/output_safety.py`, called from `build.run_build` right after the Pandoc gate and before
figures/staging/Pandoc, and again just before the atomic copy):

| Code | Rule |
|---|---|
| `L2W-E-OUTPUT-001` | the target name does not end in `.docx` |
| `L2W-E-OUTPUT-002` | the target is canonically one of the inputs: every resolved source-graph node (main, `\input`/`\include`, local `.sty`/`.cls`, bibliography, graphics, pgfplots data), the configuration file, the configured CSL, `bibliography.files`, `staging.extra_files` |
| `L2W-E-OUTPUT-003` | the target exists and is not a regular file holding a ZIP package |
| `L2W-E-OUTPUT-004` | the target is inside the project but outside the configured output directory |
| `L2W-E-OUTPUT-005` | the configured output directory contains build inputs (not a generated-output directory) |

Canonical comparison: absolute path, `..` collapsed, symlinks resolved (`realpath`), and case folded when the file
system is case-insensitive (Windows, macOS default; a parameter makes the semantics testable on Linux). A refusal
raises `OutputSafetyError` (a `BuildError`, so exit code 2). The atomic copy (`_atomic_copy`, temp file +
`os.replace`) is unchanged.

**Tests** (`tests/test_output_safety.py`, 19, all pass): `--output main.tex` (all four reasons), `refs.bib`,
`local.sty`, `chapters/one.tex`, `fig.png`, the configuration file, existing `.txt`, existing non-ZIP `.docx`, a
directory, safe targets (outside, previous DOCX), the configured output directory inside the project, an output
directory holding sources, a relative alias (`../chapters/../main.tex`), case variants under Windows semantics, a
symlink to a source, a symlinked directory into the project, refusal before any conversion (figures stage
never called, no workspace), and the CLI exit code 2 with the source intact.

## 2. F-04: reference loss per occurrence

**Design.** `labels.plan_reference_markers` writes every literal label of `\ref`, `\eqref`, `\autoref`, `\cref`,
`\Cref`, `\vref` in the live source as `l2wocc<n>:<label>` in the staged copy (edit kind `reference-marker`, recorded
in `transformations.json`). Comma lists give one occurrence per label. The filter (`l2w_semantic.lua`,
`split_occurrence`) strips the prefix before resolving, so link texts, targets and diagnostics are unchanged, and
records `occurrence = n` in the filter report. `build._lost_references` reports every marked `n` missing from the
report as `L2W-E-REF-012` at the original source location.

The identity is carried by the reader itself, so no rendered text is compared. It is independent of the command,
the label and other references to the same label.

Not marked, so not checked: references inside macro/environment definition bodies (text until used), non-literal
labels (`#1`, macros), and arguments altered by comment masking. Markers covered by another staged rewrite (e.g. a
TikZ picture replaced by its SVG) are dropped from the check; a marker that would partially overlap another rewrite
is dropped too, so `apply_edits` never refuses the plan. References generated by the filter carry no occurrence and
are ignored. Documented in `docs/architecture.md`.

**Evidence.** Harness build of `\Cref{sec:a} … \ref{sec:a}` with Pandoc 3.1.3: before → OK; after →
`L2W-E-REF-012 \Cref{sec:a} … main.tex:4`; the filter report shows the survivor as occurrence 2. Thesis-like fixture:
both dropped `\Cref` reported at lines 46 and 96.

**Tests** (`tests/test_reference_occurrences.py`, 11): acceptance cases A (`\Cref{a}` dropped, `\ref{a}` survives →
error at the `\Cref` line), B (one of two `\ref{a}` dropped → error, either one), C (unused macro body → no
occurrence), D (three survivors → pass), E (all six commands survive → pass); comma lists; generated references
ignored; overlapping/enclosing rewrites unchecked; non-literal labels unmarked; real Pandoc + filter report
occurrences; marked and unmarked labels give identical link text and target. The phase-1 set-based tests were
replaced.

## 3. Probe failure semantics

New `PandocCompat.PROBE_ERROR` with diagnostic `L2W-E-PANDOC-004`: "the Pandoc compatibility probe could not run (…);
pandoc X was not assessed (this is a tool/probe error, not a verdict on the Pandoc version)".

| Probe outcome | Classification |
|---|---|
| timeout, launch error, killed by a signal (negative exit), probe setup `OSError` | PROBE_ERROR |
| Pandoc ran and exited non-zero on the valid probe document | UNSUPPORTED (observed) |
| exit 0 with output that is not a JSON AST / AST differing from the pinned facts / failing capability | UNSUPPORTED (observed) |
| observed incompatibility and an aborted step together | UNSUPPORTED (the observation is evidence) |

`build` refuses both (exit code 2) with different messages. `doctor` shows a probe error as `UNTESTED`, not
`INCOMPATIBLE`. Tests: `tests/test_probe_semantics.py` (21). The installed Pandoc 3.1.3 is still UNSUPPORTED on
observed facts (`\linewidth` percentage, equation wrapper, `\Cref`).

## 4. Pandoc API-version rule

`PINNED_API_VERSIONS = ((1, 23),)` (was `(1, 23, 1)`). pandoc-types follows the Haskell PVP: changes a JSON
consumer can observe (new or changed element types, e.g. `Figure` in 1.23) bump the first two components, and a third
component only adds API. The narrowest justified rule is therefore the major series. It only stops a compatible AST
from being rejected outright: the reader probe still checks the pinned AST facts, and certification stays per Pandoc
version (`CERTIFIED_VERSIONS = ((3, 12),)`, unchanged). Synthetic tuples tested: accepted `1.23`, `1.23.1`,
`1.23.1.2`, `1.23.2`, `1.23.9.1`; rejected `1.22.2.1`, `1.24`, `1.24.0`, `2.23`, `1.2.3`. No new Pandoc version is
certified.

## 5. R3 flattening design

```
staged source graph (validated) ──flatten──► l2w-flattened.tex + source map
        ──► pandoc --sandbox -f latex -t json      (confined reader: the only stage that interprets TeX)
        ──► AST resource check (Image targets)      (structured data, no TeX interpretation)
        ──► pandoc -f json -t docx + filters, citeproc, reference.docx   (unchanged writer)
```

`src/latex2word/confinement.py` (experimental; enabled only by `LATEX2WORD_EXPERIMENTAL_CONFINED_READER=1`):

| Construct | Prototype behaviour |
|---|---|
| `\input`, `\include` resolved by the graph | replaced by the staged text, recursively (cycle/depth guard). `\include` is inlined like Pandoc's reader does: no page break added |
| local `.sty` resolved by the graph | inlined into the preamble between `\makeatletter`/`\makeatother`; package options are not applied (Pandoc ignores them too) |
| `\usepackage{amsmath,mine}` (local mixed with system packages) | **refused loudly** (`FlattenError` → `L2W-E-CONFINE-004`). Production accepts it, so this is a gap to close before integration |
| local `.cls` | kept as written (Pandoc does not load classes) |
| commented / unresolved / dynamic includes | not expanded; dynamic/outside ones are already graph errors; anything the reader still tries to open is refused by `--sandbox` → `L2W-E-CONFINE-001` with the original `file:line` |
| bibliography | passed to the writer stage as before (`--bibliography`, `--csl`) |
| figures | the reader never reads images; every `Image` target in the AST must be a relative path to an existing staged file (`L2W-E-CONFINE-002`); SVGs from the TikZ pipeline are staged assets |
| TikZ/pgfplots data | unaffected: consumed by the separate TeX process (F-07), never by Pandoc |
| custom macros | defined in the main file or an inlined `.sty`: expanded by the reader as before |
| front matter | unchanged (metadata adapters work on the staged graph before Pandoc) |
| source locations | `FlatSource.locate(offset)` maps every flattened character to its original file and line |

## 6. `--sandbox` experiment matrix (Pandoc 3.1.3, Debian build)

Full commands and outputs: `logs/phase2/sandbox-full-pipeline.md`, `logs/phase2/sandbox-reader-stage.md`.

| Question | Observation |
|---|---|
| DOCX writer under `--sandbox` | **fails for every input**, even a self-contained file and with `--reference-doc`: "Could not find data file data/data/docx/[Content_Types].xml". This build reads its data files from disk and the sandbox blocks them. A Pandoc with embedded data files may behave differently: re-check locally |
| 1. What can the LaTeX reader access? | only the input file on its command line. Refused: relative `\input` (legitimate chapters, so flattening is required), absolute `\input`, `\include`, `\lstinputlisting`, `\inputminted`, `\subfile`, `\let` aliases, both residual attacks; all reported as "Could not load include file …". A local `.sty` is refused **silently** (macro lost, no warning) |
| `--bibliography`/citeproc in the reader run | allowed (files named on the command line) |
| 2. What can the DOCX writer access? | in the unsandboxed writer stage: images by the paths in the AST (`local.png` embedded), the reference document, bibliography and CSL named on the command line |
| 3. What can Lua filters access? | **everything**: with `--sandbox`, a filter's `io.open` read the outside canary and `pandoc.mediabag.fetch` fetched an outside PNG. `--sandbox` does not confine filters. latex2word's filters are trusted code that read only the build's own configuration |
| 4. What breaks in the current pipeline? | a single `--sandbox` run breaks the DOCX writer (above), `\input` of chapters and local packages. Hence the two-stage design: confined reader, unconfined JSON writer |
| resource-path / staged temporary files | the reader never needs them (it reads no images); the writer stage keeps `--resource-path <staged main dir>` |

## 7. Results against the F-02 attacks

`logs/phase2/r3-attacks.md` (script `tools/audit/phase2/r3_attacks.py`). Canary file outside the project; "alone" =
flatten + confined reader on the attack project with no deny-list in front.

| Attack | Production build | Experimental build: AST / preliminary / final | Confined reader alone |
|---|---|---|---|
| `\input{\macro}` | blocked (`dynamic-path`) | absent / absent / absent | clean, `CONFINE-001` |
| `\includegraphics{\macro}` | blocked | absent | clean, `CONFINE-002` |
| `\let\inp\input` | blocked (`unsupported-syntax`) | absent | clean, `CONFINE-001` |
| `\let\ig\includegraphics` | blocked | absent | clean, `CONFINE-002` |
| wrapper `\newcommand{\inp}[1]{\input{#1}}` | blocked | absent | clean, `CONFINE-001` |
| `\lstinputlisting`, `\inputminted`, `\subfile` | blocked | absent | clean, `CONFINE-001` |
| `\includesvg` | blocked | absent | clean, `CONFINE-002` |
| local `.sty` macro reading outside | blocked (`outside-root`) | absent | clean, `CONFINE-001` |
| `\usepackage{\macro}` | blocked | absent | clean (sandbox refused silently) |
| literal `\include{/abs}` | blocked | absent | clean, `CONFINE-001` |
| **`\iffalse … \ifx … \fi \input{/abs} \fi`** | **leaks** | **blocked by `CONFINE-001`; canary absent everywhere** | clean, `CONFINE-001` |
| **`filecontents` body with `\input{/abs}`** | **leaks** | **blocked by `CONFINE-001`; canary absent everywhere** | clean, `CONFINE-001` |

Pinned by `tests/test_confinement_prototype.py` (9 tests). Each attack test also confirms that the unconfined
reader **does** read the canary, so the attack is real.

**Compatibility of the experimental path** (`logs/phase2/r3-compat.md`, script `tools/audit/phase2/r3_compat.py`):
on `clean_article`, `local_class` (.sty/.cls), `citations`, the new `multifile_project` (nested `\input`, `\include`,
local `.sty` macros, bibliography + citeproc, PNG figure, cross-references, display math) and `generic_article` /
`report_structure` (both fail identically on Pandoc 3.1.3 AST issues), the final `document.xml` is
**byte-identical** and the diagnostics are identical in both modes. `minimal_project` is not comparable (TikZ figure,
no TeX).

## 8. Is R3 ready for integration?

**Recommendation: NEEDS_LOCAL_CERTIFICATION.** The architecture is compatible: same document on every comparable
fixture, both residual attacks closed by structure rather than by the deny-list. It is not ready to replace
production because:

1. sandbox semantics were measured only with a Debian Pandoc 3.1.3 build; Pandoc 3.12/3.13 (official builds, embedded
   data files) must be re-measured with `tools/audit/phase2/` and `tests/test_confinement_prototype.py`;
2. `\include` equivalence (no page break, like Pandoc's reader) is verified only against 3.1.3;
3. mixed `\usepackage{system,local}` lists are refused by the flattener but accepted by production (feature gap);
4. a silently refused `\usepackage` (no warning from the sandbox) would lose macros without a diagnostic; with the
   flattener every resolvable local package is inlined, so this only affects packages that are already graph errors,
   but an invariant (macro use vs definition) would make it explicit;
5. no TikZ fixture could be compared (no TeX);
6. Lua filters remain unconfined by design (trusted code); TeX isolation (F-07) is separate.

## 9. F-07: TeX threat boundary

Written to `SECURITY.md` ("TeX for figures"):
- **Process boundary:** `render_tikz` runs `latex` with the documented flags, `cwd` = the staged main directory and `-output-directory` = the render directory, then `dvisvgm`.
- **Files the TeX process needs:** the generated `.tex`, staged data below the main directory, and the TeX distribution (all read); `.dvi`/`.log`/`.aux` in the render directory (write); nothing else.
- **Why `openin_any=p` is not the mechanism:** it is TeX Live-specific, overridable, still allows reads from the distribution trees, and does nothing for `dvisvgm`.
- **Planned design (not implemented, untestable here):** OS-level confinement with a read-write render directory, a read-only staged project and TeX distribution, and no network.

## 10. F-11: CSL / biblatex style mismatch

Declared styles are read from `\usepackage[style=…|bibstyle=…]{biblatex}` and `\bibliographystyle{…}`. The CSL's
identity is its file stem, the last segment of `<id>`, and `<title-short>` (hardened XML parser). Matching uses only
the profile table `bibliography.style_csl` (`profiles/generic.yaml`, schema-validated, inherited). Style names live
in profile data because the architecture tests forbid them in engine code. The first implementation violated that,
was caught by the attribution run, and was moved.

| Situation | Diagnostic |
|---|---|
| declared style, no CSL | `L2W-W-BIB-014` (Pandoc default chicago-author-date is used) |
| declared style, CSL of another style | `L2W-W-BIB-015` |
| declared style with no table entry and a CSL | `L2W-W-BIB-016` (cannot be verified) |
| declared style matches the CSL | `L2W-I-BIB-015` |
| declared `chicago-authordate`, no CSL | nothing (Pandoc's default is that style) |

**WARNING, not ERROR:** the documented fidelity contract treats CSL formatting divergence as a limitation, not as
content loss (errors are reserved for lost or corrupted content), and `validation.fail_on: warning` lets a project
make it blocking. No CSL was downloaded or vendored. Tests: `tests/test_bibliography_style.py` (12).

## 11. Portability (F-09)

`test_distinct_files_whose_names_differ_only_in_case_are_refused` now creates the second spelling when the file system
is case-sensitive. `test_lookup_asks_for_bare_names…[windows-exe|windows-cmd-shim]` now uses `ntpath.basename`
(Windows semantics on every host). No production code changed. No third platform defect was found; the reviewer's
other caveat (Lua tests load the filter from the repository path) is about packaging, not platform.

## 12. Math generated tests (R9)

`tests/test_math_rows_generated.py`: **584 generated cases**. The dimensions are 9 environments (align, align*,
aligned, split, gather*, alignat*, flalign*, array{rl}, eqnarray*), 4 row breaks (`\\`, `\\*`, `\\[2pt]`, `\\*[2pt]`),
5 row starts (plain, `[x,y]` after a spacing argument, after a space, after a newline, `\left(…\right)`), escaped `\&`
on/off, and first row plain/bracketed. The pipeline is `l2w_math.analyze` on Pandoc-3.12-shaped strings → texmath →
OMML. The oracle is an independent linearisation including delimiter characters.

Invariants:
- every source token is present;
- `2pt` never appears;
- the number of `&` equals the number of `\&`;
- no case raises an error.

**All 584 pass.** Against the pre-audit filter (`35547d4`) the same matrix reports **277 failing cases**
(`logs/phase2/r9-against-baseline-filter.log`), so it would have caught F-01.

One mistake was found in my own generator: it produced `\\[x,y]`, which is LaTeX's spacing-argument syntax, not
content. It was removed before the run above. Kernel environments get no bracketed content after a space, because
that isn't valid LaTeX there. **F-12** is pinned as `L2W-E-MATH-003` for `align`/`gather` with two numbered rows, and
not implemented.

## 13. README / security contract

- `SECURITY.md` (new): trust boundary sentence, guarantees for trusted input, the two open reader leaks, the TeX boundary.
- `README.md`: trust-boundary banner. "Constructs … never silently dropped" is replaced by what the checks cover and that they cannot prove universality. The 160+ page validation is attributed to the maintainer as private and not part of the repository's tests. The Pandoc policy now matches the build gate. "All selected tests pass" is limited to the certified toolchain. The safety bullet covers output protection and says it is not a sandbox.
- `docs/known-limitations.md`: new Security section; CSL warnings; F-12; Pandoc probe semantics.

## 14. Test evidence and remaining blockers

**Suite on Pandoc 3.1.3** (`logs/phase2/phase2-attribution.txt`, JUnit `logs/phase2/phase2-junit.xml`):

| Category | Count |
|---|---|
| passed | 1074 (skipped 9) |
| unit-test failures (no Pandoc involved) | **0** |
| expected build-gate failures (`L2W-E-PANDOC-003`, Pandoc 3.1.3 refused by design) | 143 |
| Pandoc-3.1.3 AST failures in tests that call Pandoc + filter directly (all already failing at baseline `35547d4`) | 25 |
| actual regressions / unexplained | **0** |

**Gate-bypass comparison** (`logs/phase2/gate-bypass-comparison.txt`): with only the Pandoc gate and the schema-order
check bypassed, so end-to-end tests run past the environment stops, the phase start had 92 failing tests and `HEAD`
has 89. **0 new**; 3 fixed (F-09). This is not evidence of behaviour on Pandoc 3.12: the 89 remaining failures are
Pandoc 3.1.3 AST differences and the missing `rsvg-convert`.

No independent reviewer was run in this phase; all verification above is my own execution.

**Blockers for local certification (owner's Windows workstation):**

1. full suite on Pandoc 3.12 and 3.13 (expected: no failures; the F-04 marker edits and the F-06 policy run in every
   end-to-end build and have not been exercised on a certified Pandoc);
2. `tests/test_confinement_prototype.py` and `tools/audit/phase2/*.py` on 3.12/3.13: sandbox semantics, writer data
   files, `\include` equivalence;
3. TikZ fixtures (`minimal_project`) in both modes; F-07 confirmation that TeX reads outside files today;
4. the private integration project and the final thesis: per-occurrence references (`L2W-E-REF-012` must stay silent
   on a valid thesis), the output policy with the real configuration, the CSL warnings with the real style;
5. Word: open without repair, update fields, review `visual_review.md`.
