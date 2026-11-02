# Executive summary

**Verdict: not release-ready for untrusted input or unattended thesis conversion; the architecture is sound and
loud-by-default, but this audit found and fixed two P0 defects that the existing suite did not catch, and the
certified toolchain could not be exercised here.**

Scope: repository `latex2word` at `35547d4` (audit start). Fixes on branch `claude/eager-archimedes-0ka8gx`
(`2d37916`, `a61debb`). Environment: Pandoc 3.1.3, no TeX, no Microsoft Word, so every claim below that
depends on Pandoc 3.12, TeX or Word is BLOCKED or NOT TESTED, not passed.

## What was found

| ID | P | One line | Status |
|---|---|---|---|
| F-01 | P0 | `\\ [x,y]` at the start of an `align`/`aligned` row silently deleted `[x,y]` from the equation, even in numbered equations | fixed, regression-tested |
| F-02 | P0 | untrusted LaTeX could make Pandoc read files outside the project into the DOCX (`\input{\macro}`, `\let` aliases, `\includegraphics{\macro}`, `\lstinputlisting`, `\inputminted`) | fixed (deny-list; residual risk documented) |
| F-05 | P1 | `build` never applied the Pandoc compatibility verdict `doctor` computes; with Pandoc 3.1.3, numbered equations lost their numbers silently | fixed |
| F-04 | P1 | a reference the reader drops (`\Cref` on 3.1.3) vanished without a diagnostic | detection added (`L2W-E-REF-012`) |
| F-03 | P2 | `\label{a_{1}}` with nested braces left TeX inside the equation | fixed |
| F-06 | P2 | `--output` can overwrite project sources | open (static) |
| F-07 | P2 | TeX may read arbitrary files while rendering TikZ | open (BLOCKED) |
| F-11 | P2 | no APA CSL shipped; biblatex `style=apa` silently becomes Chicago | open |
| F-12 | P2 | two numbered rows in one `align` is a hard error | documented limitation |
| F-08, F-09 | P3 | environment / test-portability issues | open |

Details, reproductions and root causes: `findings.md`. Security: `security.md`.

## What holds up (evidence)

* Loud failure by design: undefined labels, unknown commands, `\tag`, `\intertext`, raw TeX all stop the build;
  output is written atomically only after validation (static + executed on fixtures).
* Subprocesses: argv only, no shell, timeouts, `-no-shell-escape` (static). Workspace deletion guarded (static).
  XML parsing hardened (static).
* Clean fixture through the pipeline (harness, Pandoc 3.1.3): independent OOXML oracle clean, OMML structure equal
  to hand-derived expectations, byte-identical across two builds.

## What is not established

* Behaviour on the certified Pandoc 3.12: **BLOCKED**. The 92 baseline failures here are explained by the
  Pandoc version and the missing tools, but "all green on 3.12" is the README's claim, not this audit's finding.
* The historical thesis numbers (1 414 equations, 54 numbered, 21 TikZ figures, 31 tables, 74 APA references):
  **NOT TESTED**. The thesis is not in the repository and no APA CSL ships. The repository itself says
  cross-reference numbers are literal text, not Word REF/SEQ fields.
* Word rendering, field update and equation editability: **NOT TESTED** (no Word).

## Test evidence

| Revision | Pandoc 3.1.3 suite |
|---|---|
| baseline `35547d4` | 1012 passed, 92 failed, 18 errors |
| after F-01..F-04 | identical failure set + 33 new passing tests |
| after F-05 | 986 passed, 141 failed, 30 errors: all 73 newly failing tests stop at the new gate refusing Pandoc 3.1.3 (attributed by JUnit XML) |

New regression tests fail on the unfixed code (`logs/regressions-before-fix.log`) and pass after.

## Independent adversarial review

_Pending — filled in from the separate reviewer's report._

## Prioritised remediation roadmap

| # | Action | Addresses | Effort |
|---|---|---|---|
| R1 | Run the full suite and `test_audit_e2e.py` on Pandoc 3.12 + TeX; run the integration suite on the original thesis | release gate, F-10 | low (needs the toolchain) |
| R2 | Confine `--output` and the default output directory outside the project | F-06 | low |
| R3 | Flatten the staged sources into one file and run Pandoc with `--sandbox` (structural fix for the F-02 class) | F-02 residual | medium |
| R4 | Restrict TeX reads (`openin_any=p`) for figure rendering after checking staged data paths | F-07 | low–medium |
| R5 | Support one number per row in `align`/`gather` (multiple numbered rows) | F-12 | medium |
| R6 | Normalise property-child order in the finaliser instead of failing on Pandoc-originated order | F-08 | low |
| R7 | Ship or require a CSL matching biblatex `style=`; warn on mismatch | F-11 | low |
| R8 | Per-occurrence reference invariant (not per label); equation-count invariant source ↔ report | F-04 limit | low |
| R9 | Property-based tests for `l2w_math.lua` (random rows/cells → linearised OMML) | F-01 class | low |
| R10 | Make the 3 platform tests portable | F-09 | trivial |
