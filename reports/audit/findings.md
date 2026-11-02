# Findings

Status vocabulary: **VERIFIED** (executed, evidence recorded), **FAILED**, **BLOCKED** (needed tool unavailable),
**NOT TESTED**, **NOT APPLICABLE**. "Static" = from reading code only.
Priorities: P0 data loss / security / serious math alteration · P1 broken core function or references ·
P2 compatibility / reliability / significant formatting · P3 minor.

Environment for every executed result: Pandoc **3.1.3** (not the certified 3.12), no TeX, no Word
(`logs/environment.txt`). Fixes: commits `2d37916` (F-01..F-04), `a61debb` (F-05) and a follow-up commit after the
independent review (§"Independent review and follow-up").

## Summary

| ID | Title | P | Reproduced | Fixed | Regression test |
|---|---|---|---|---|---|
| F-01 | Alignment row starting with `[` silently deleted from equations | P0 | VERIFIED | yes (incl. review follow-ups) | `test_audit_regressions.py::test_f01_*`, `::test_f01b_*` |
| F-02 | Untrusted LaTeX makes Pandoc read files outside the project into the DOCX | P0 (untrusted input) | VERIFIED | **partially**: 10 vectors closed; 2 known scanner/reader mismatches remain open | `::test_f02_*`, `::test_f02b_*` |
| F-03 | `\label{..{..}..}` with nested braces not stripped from math | P2 | VERIFIED | yes | `::test_f03_*` |
| F-04 | Reference command dropped by the reader without diagnostic | P1 | VERIFIED (3.1.3) | detection added (per label, not per occurrence) | `::test_f04_*`, `::test_f04b_*` |
| F-05 | `build` ignores the Pandoc compatibility verdict that `doctor` computes; equation numbers lost silently | P1 | VERIFIED | yes; probe now checks the equation wrapper and `\Cref` | `::test_f05_*`, `::test_f05b_*` |
| F-06 | `--output` may overwrite project sources; default output lies inside the project | P2 | static | no | — |
| F-07 | TeX may read arbitrary files while rendering TikZ (`openin_any`) | P2 | BLOCKED (no TeX) | no | — |
| F-08 | Every build fails on Pandoc 3.1.3 (its own `styles.xml` out of schema order) | P3 | VERIFIED | no (env.) | — |
| F-09 | 3 platform tests are not portable to Linux | P3 | VERIFIED | no | — |
| F-10 | Historical claims not reproducible from the repository | — | see §F-10 | — | — |
| F-11 | No APA CSL shipped; default is Chicago author-date | P2 | static | no | — |
| F-12 | Two numbered rows in one `align` is a hard error | P2 | VERIFIED (filter unit) | no (documented limitation) | existing |

---

## F-01 — Alignment row starting with `[` silently deleted (P0)

1. **Minimal input**: `\begin{aligned} a &= b \\ [x,y] &= z \end{aligned}` (also `align`, `gather`, `split`,
   `\\* [c]`).
2. **Expected**: two rows, `a = b` and `[x,y] = z`. amsmath's `\\` reads its optional spacing argument with
   `\new@ifnextchar`, which does **not** skip spaces, so in amsmath environments `\\ [x,y]` ends the row and `[x,y]`
   is content. (Kernel `array`/`eqnarray` use `\@ifnextchar`, which does skip spaces.)
3. **Actual (before fix)**: filter output `\begin{array}{rlc} a & = b & \\ & = z & \end{array}`, zero
   diagnostics; with `\begin{align} … \label{e}` the equation stays numbered and labelled, so the result looks
   valid in every report.
4. **Root cause**: `top_split` in `filters/l2w_math.lua` skipped whitespace after `\\` and discarded any `[...]`.
5. **Evidence**: `pandoc lua` run of `M.analyze` (audit transcript); the regression tests fail on `35547d4`
   (`logs/regressions-before-fix.log`). Differential check: texmath without the filter **preserves** `[x,y]` (as an
   OMML `m:d` delimiter). An earlier note of mine that texmath also lost it was wrong (the extractor read only `m:t`).
6. **Correction**: the bracket is consumed only when it directly follows `\\`/`\\*`, except in kernel
   environments; a rebuilt row that starts with `[` gets a `{}` guard, because texmath applies kernel array rules to
   the emitted `array` (verified: `\\ [x,y]` inside `array` loses the content in texmath; `\\ {}[x,y]` keeps it).
7. **Regression tests**: 6 tests, OMML linearised independently (including `m:begChr`), plus direct module tests
   with Pandoc-3.12-shaped strings. Controls: `\\[2pt]` and kernel `array`/`eqnarray` keep spacing semantics.

## F-02 — Files outside the project root reach the DOCX (P0 under an untrusted-input threat model)

1. **Minimal inputs** (each verified to leak a canary file through a real build before the fix):
   `\newcommand{\p}{/abs/secret}\input{\p}` (warning only) ·
   `\newcommand{\p}{/abs/private.png}\includegraphics{\p}` (image embedded byte-identical, SHA-256 match) ·
   `\let\inp\input \inp{/abs/secret}` and `\let\ig\includegraphics` (warning only; found while testing the first fix) ·
   `\lstinputlisting{/abs/secret.tex}` (warning only) · `\inputminted{tex}{/abs/secret.tex}` (**no diagnostic**) ·
   `\subfile{/abs/secret.tex}` (blocked, but text was already in `preliminary.docx`).
2. **Expected**: the source graph's containment check (literal `\input`/`\include`/`\usepackage` outside the root →
   error) applies to every way Pandoc opens files.
3. **Actual**: the scanner could not resolve these paths and only warned; Pandoc then expanded the macro or
   followed the alias and read the file.
4. **Root cause**: two parsers. latex2word's scanner does not expand macros; Pandoc's reader does.
5. **Evidence**: canary `TOPSECRETCANARY42` found in `preliminary.docx` for each vector (audit transcript);
   regression tests fail on `35547d4`.
6. **Correction**: in `source_graph.py`, dynamic and braceless `\input`/`\include`/`\includegraphics`, and
   `\lstinputlisting`/`\inputminted`/`\subfile`, are ERROR findings, so the build stops before Pandoc runs.
   Commands Pandoc does not open (`\verbatiminput`, `\import`, `\InputIfFileExists`, tested) stay warnings.
   **Compatibility**: documents using macro-built `\input` paths (e.g. `\input{\chapdir/intro}`) now fail. Before,
   they lost that content anyway (target not staged) or read from an unchecked path. The changed assertion in
   `tests/test_source_graph.py::test_dynamic_and_unsupported_constructs_produce_diagnostics_not_guesses` is marked.
7. **Residual risk**: the defence is a deny-list over a scanner that does not expand TeX. Further Pandoc-honoured
   indirections may exist. Recommended defence in depth (roadmap R3): run Pandoc on a flattened single file with
   `--sandbox`.

## F-03 — Nested-brace label left in the equation (P2)

`\begin{equation} x = 1 \label{eq:a_{1}} \end{equation}`: the pattern `\label%s*{([^{}]*)}` did not match, so
`\label{eq:a_{1}}` stayed in the TeX and texmath failed ("Could not convert TeX math … rendering as TeX"). The label
was not registered, so references to it became `??` (loud). Fix: balanced match `%b{}`. Test: `test_f03_*`.

## F-04 — Reference dropped by the reader without diagnostic (P1)

1. **Input**: `\Cref{tab:larga}` (repository fixture `tests/fixtures/semantic/report_structure`).
2. **Expected**: a reference, or an error.
3. **Actual with Pandoc 3.1.3**: `\Cref{x}` vanishes from the AST (`pandoc -t native`); the sentence reads
   "… resume;" with nothing in front. No diagnostic, because `texprobe.py` treats `cref`/`Cref` as known.
4. **Root cause**: version-dependent reader behaviour plus no invariant between source references and converted
   references.
5. **Correction**: `build._lost_references` compares labels referenced by `\ref`/`\eqref`/`\autoref`/`\cref`/
   `\Cref`/`\vref` in the staged source with the filter report and raises `L2W-E-REF-012` at the original source
   location. Verified on the fixture: both dropped `\Cref` are reported (lines 46 and 96).
6. **Limit**: set-based (per label), not per occurrence: if a label is referenced twice and only one reference is
   dropped, this is not detected.

## F-05 — `build` ignores the Pandoc classification `doctor` reports (P1)

1. **Input**: `\begin{equation} E = mc^2 \end{equation}` plus an unlabelled `align`, built with Pandoc 3.1.3.
2. **Expected**: equation numbers (1)–(3), or a refusal. `latex2word doctor` classifies 3.1.3 as **UNSUPPORTED**
   (`L2W-E-PANDOC-003`, `logs/doctor.txt`).
3. **Actual**: the build ran with no Pandoc diagnostic; the summary showed "Equations: -" (all three numbers lost
   silently). Pandoc 3.1.3 strips `\begin{equation}` from the math string, which the filter uses to detect numbering.
4. **Root cause**: `pandoc_compat.assess_pandoc` was called only from `toolchain.py`. Its warning text ("the build
   continues …") shows the build was meant to use it.
5. **Correction**: `run_build` runs the same assessment. UNSUPPORTED raises `BuildError` (exit code 2, same as an
   unusable Pandoc); TESTED_UNCERTIFIED adds the existing warning; SUPPORTED adds nothing.
6. **Consequence in this container**: 73 more tests now stop at the gate (all 73 attributed by JUnit XML, see
   `logs/after-fix-new-failures.txt`). On Pandoc 3.12 these should be unaffected: **BLOCKED**, not verified.

## F-06 — Output path can overwrite project files (P2, static)

`build._finish` writes to `--output <path>` without checking that it is outside the project root or not a source
file. `latex2word build p.yaml --output main.tex` would replace `main.tex` with a DOCX once a build succeeds. Also
the default `output.directory` is `<project>/output`, which contradicts the README claim that "the LaTeX project is
never modified". NOT TESTED dynamically (no build can succeed under 3.1.3 without the harness). Proposed fix: refuse
an output inside the project root unless it is the configured output directory, and refuse existing non-`.docx`
targets.

## F-07 — TeX file reads during figure rendering (P2, BLOCKED)

TikZ code is compiled with `-no-shell-escape` (verified statically, `tikz_render.py:22`), but TeX Live's default
`openin_any=a` lets `\input{/etc/passwd}` inside a `tikzpicture` render the file into the SVG. Not testable without
TeX. Proposed fix: set `openin_any=p` in the latex environment after checking that staged pgfplots data paths need no
`..`; MiKTeX needs its own policy.

## F-08 — Pandoc 3.1.3 output always fails schema-order validation (P3, environmental)

`preliminary.docx` from Pandoc 3.1.3 already has 15 `w:style`/`w:rPr` containers out of schema order (e.g.
`Heading4 rPr: i, bCs, sz, szCs`) before latex2word edits anything. `L2W-E-LAYOUT-090` correctly flags them. With F-05
such a Pandoc is refused anyway. The finaliser could sort children into schema order (roadmap R6).

## F-09 — Non-portable tests (P3)

`test_platform_paths.py`: `test_distinct_files_whose_names_differ_only_in_case_are_refused` needs both spellings
to exist (case-insensitive FS); `test_lookup_asks_for_bare_names_…[windows-exe|windows-cmd-shim]` calls
`os.path.basename` on Windows paths on POSIX. These are test bugs, not product defects. Fix: skip on case-sensitive
FS / use `ntpath.basename`.

## F-10 — Historical reference results

| Claim (prompt) | Status | Reason |
|---|---|---|
| 1 414 editable equations, 54 numbered | NOT TESTED | thesis not in the repository; `test_integration_reference.py` needs `LATEX2WORD_INTEGRATION_PAPER_ROOT` + private expectations |
| 21 TikZ/PGFPlots vector figures | BLOCKED/NOT TESTED | no TeX in the container; thesis absent |
| 2 logos, 31 tables, front matter, annexes | NOT TESTED | thesis absent; synthetic fixture covers the constructs (report_structure) |
| 74 APA bibliography entries | NOT TESTED; not reproducible from repo contents | no APA CSL in the repository (F-11) |
| Word fields for TOC/LOF/LOT | VERIFIED structurally (TOC field instruction present, fields balanced; harness build) | Word field update NOT TESTED (no Word) |
| Cross-references as Word fields | **contradicted by the repository**: numbers and reference texts are literal text with hyperlinks, by design (README, `docs/m4-layout.md`) | — |

## F-11 — No APA style available out of the box (P2, static)

`bibliography.py` uses `config.csl or profile_csl`. Neither built-in profile sets a CSL and no `.csl` ships, so
`\usepackage[style=apa]{biblatex}` is rendered with Pandoc's default CSL (Chicago author-date). Nothing reports
this mismatch. Proposed: warn when biblatex `style=` differs from the CSL in use.

## F-12 — Two numbered rows in one `align` is an error (P2, limitation)

`\begin{align} a&=b \\ c&=d \end{align}` (two numbered rows) yields `L2W-E-MATH-003` (explicit failure, not
silent). This is common in theses; such documents fail until every multi-row numbered environment is split.
Documented in `docs/pandoc-ast-findings.md`. Roadmap R5.

## Independent review and follow-up

A separate agent with its own context reviewed `2d37916` and `a61debb` read-only and tried to invalidate each fix by
executing builds and module calls (report kept in the session; its claims were re-executed here before acting).

| Fix | Reviewer verdict | Re-verified here | Follow-up |
|---|---|---|---|
| F-01 | partially fixed | yes | `eqnarray` rows are split twice, so `\\[2pt] [x]` still lost `[x]` → guard after the first split. `alignat{2}` lost its column count on rebuild and then ate the first cell (pre-existing) → argument kept. Guard `{}` added where texmath does not need it, giving an extra zero-width run in Word (minor regression) → only rows after the first in a rebuilt array, and kernel wrapper rows |
| F-02 | not fixed against the threat model | `.sty` and `\includesvg` leaks re-executed (canary embedded) | local `.sty` files now get the containment checks (Pandoc loads them); macro-built `\usepackage{\x}` is an error; `\includesvg` is an error. False errors removed: `\input@path` (internal name), `\renewcommand{\includegraphics}` (definition target) |
| F-03 | confirmed fixed | — | — |
| F-04 | partially fixed | false positive re-executed | a `\ref` inside an **unused** macro body made a valid build fail → references inside definition bodies are excluded |
| F-05 | partially fixed | yes | the probe rejected 3.1.3 only through the unrelated `\linewidth` fact → probe also checks "equation environment kept in the math string" and "`\Cref` → `ref+Label`" (the facts pinned in `tests/test_pandoc_ast.py:67,82`) |

Regression tests for the follow-ups: 11 tests, all failing on the pre-follow-up code
(`logs/followup-before-fix.log`) and passing after (`logs/followup-after-fix.log`); full-suite failure set unchanged.

**Still open (reported by the review, not fixed):**

* F-02 residual: `\iffalse … \ifx … \fi \input{/abs} \fi`. latex2word's masker treats `\ifx` as nested and masks
  up to the second `\fi`; Pandoc 3.1.3 ends at the first and reads the file. Also, a `filecontents` body is masked by
  latex2word but parsed by Pandoc. Both are scanner/reader disagreements: the deny-list approach cannot close this
  class. Only R3 (single flattened input + `pandoc --sandbox`) can. **Untrusted input remains unsupported.**
* F-02 trade-offs (fail-safe errors on valid documents): `\let\oldinput\input` is an error (indistinguishable from
  the leaking alias). `\newcommand{\fig}[1]{\includegraphics{#1}}` is an error; verified that at `35547d4` the same
  document already failed (`L2W-E-FIG-007`, image not staged), so the outcome is unchanged and only the message
  is earlier.
* F-04: per-label check; `\Cref{a}` dropped while `\ref{a}` survives elsewhere is not detected (executed by the
  reviewer: "As shows, see also 1.").
* F-05: a probe timeout is reported as UNSUPPORTED ("timed out"), which is misleading for transient slowness. The API
  pin `(1,23,1)` would reject a future pandoc-api-version 1.23.2+ (inferred, not tested).
* Test caveat: the F-01/F-03 Lua tests load the filter from the repository path, so they test the working tree, not
  an installed package.

## Rejected hypotheses (tested, no defect)

* Subprocess injection: every tool runs as an argv vector, `shell=False`, mandatory timeout (`process.py`). Static.
* Workspace deletion: `rmtree` only on a directory whose ownership marker names it, never on links, root, home,
  the project or its parents (`workspace.py`). Static.
* XXE in OOXML parsing: `lxml` parser with `resolve_entities=False, no_network=True, load_dtd=False`. Static.
* SVG entities: documents containing `<!ENTITY` are refused before parsing. Static.
* Literal `\include{/abs}` and `\usepackage{/abs/x}`: blocked (outside-root ERROR). VERIFIED.
* `\[ … \]` math, escapes `\% \& \$ \# \_ \{ \}`, accented names: preserved. VERIFIED (harness build, `test_audit_e2e.py`).
* `\&` inside alignments: kept as a literal `&`; structural `&` removed. VERIFIED (filter unit).
