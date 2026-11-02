# Executive summary

**Verdict after phase 2: CLOUD REMEDIATION PARTIAL — ARCHITECTURAL WORK REMAINS.** Not release-ready.
Everything implementable without Pandoc 3.12/3.13, TeX and Word was fixed and tested. The two remaining Pandoc-reader
leaks are closed only by an experimental confined-reader path that needs local certification. TeX isolation for
TikZ (F-07) is designed but not implemented. latex2word is documented as requiring trusted LaTeX input.

## Phase 2 (structural hardening), in short

| Issue | Status | Evidence | Needs local Windows/TeX/Word validation? |
|---|---|---|---|
| F-06 output overwrite | fixed: canonical comparison with every input, refused before conversion | `test_output_safety.py` (19); reproduced overwrite before | yes: real configuration, Windows paths/junctions |
| F-04 reference loss | fixed per occurrence (staged markers through the filter) | `test_reference_occurrences.py` (11); harness before/after | yes: Pandoc 3.12/3.13, thesis must stay free of `REF-012` |
| F-05 probe semantics | fixed: `PROBE_ERROR`/`L2W-E-PANDOC-004`; API pin `(1,23)` | `test_probe_semantics.py` (21) | yes: probe on 3.12 (SUPPORTED) and 3.13 |
| F-02 reader leaks | production: 2 open; experimental confined reader closes all 14 vectors | `test_confinement_prototype.py` (9), `logs/phase2/r3-*.md` | yes: sandbox semantics on 3.12/3.13 |
| R3 integration | NEEDS_LOCAL_CERTIFICATION | 5 comparable fixtures byte-identical in both modes | yes |
| F-07 TeX reads | open; boundary and design in `SECURITY.md` | static | yes (TeX) |
| F-11 CSL mismatch | fixed as detection (warnings) | `test_bibliography_style.py` (12) | yes: real thesis style/CSL |
| F-09 portability | fixed (tests) | `test_platform_paths.py` | yes: run on Windows |
| R9 math matrix | 584 cases pass; 277 fail on the pre-audit filter | `test_math_rows_generated.py` | yes: OMML rendering in Word |
| F-12 | documented limitation, loud | pinned test | no |
| Suite (Pandoc 3.1.3) | 0 unit failures, 143 expected gate failures, 25 baseline AST failures, 0 unexplained; gate-bypass run: 0 new failures | `logs/phase2/` | full suite on 3.12/3.13 |

Phase-1 summary follows (phase-1 verdict: not release-ready; superseded by the verdict above).

Scope of phase 1: repository at `35547d4`; fixes `2d37916`, `a61debb`, `dd0b39b`. Same environment (Pandoc 3.1.3, no
TeX, no Word): every claim that depends on Pandoc 3.12, TeX or Word is BLOCKED or NOT TESTED, not passed.


## What was found

| ID | P | One line | Status |
|---|---|---|---|
| F-01 | P0 | `\\ [x,y]` at the start of an `align`/`aligned` row silently deleted `[x,y]` from the equation, even in numbered equations | fixed, regression-tested |
| F-02 | P0 | untrusted LaTeX could make Pandoc read files outside the project into the DOCX (`\input{\macro}`, `\let` aliases, `\includegraphics{\macro}`, local `.sty` macros, `\includesvg`, `\lstinputlisting`, `\inputminted`, `\usepackage{\macro}`) | **partially fixed**: 10 vectors closed, 2 scanner/reader mismatches (`\iffalse` nesting, `filecontents`) still leak |
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
| after review follow-up | 997 passed, failure set identical to the F-05 run |

New regression tests fail on the unfixed code (`logs/regressions-before-fix.log`) and pass after.

## Independent adversarial review

A separate agent with its own context reviewed the fix commits read-only and executed attacks against them. Verdicts:
F-01 partially fixed, F-02 **not fixed** against the threat model (5 working leaks), F-03 confirmed, F-04 partially
fixed with a false positive, F-05 partially fixed. I re-executed its key claims before acting. All were reproduced.

Follow-up fixes closed 3 of the 5 leaks (local `.sty`, `\includesvg`, macro `\usepackage`), the F-01 gaps, the F-04
false positive, and two new false errors on valid documents. They also made the Pandoc probe check the facts that
matter. 11 new regression tests fail before and pass after. The two remaining leaks are scanner/reader disagreements
(`\iffalse` nesting, `filecontents`), which a deny-list cannot close. **Untrusted LaTeX input is therefore not
supported** until roadmap R3 (single flattened file + `pandoc --sandbox`) is done. Limitation: the reviewer is the
same model family with a fresh context, not a human or a different tool.

## Prioritised remediation roadmap

| # | Action | Addresses | Effort |
|---|---|---|---|
| R1 | Run the full suite and `test_audit_e2e.py` on Pandoc 3.12 + TeX; run the integration suite on the original thesis | release gate, F-10 | low (needs the toolchain) |
| R2 | Confine `--output` and the default output directory outside the project | F-06 | low |
| R3 | Flatten the staged sources into one file and run Pandoc with `--sandbox` (structural fix for the F-02 class; required before accepting untrusted input) | F-02 residual (`\iffalse`, `filecontents`) | medium |
| R4 | Restrict TeX reads (`openin_any=p`) for figure rendering after checking staged data paths | F-07 | low–medium |
| R5 | Support one number per row in `align`/`gather` (multiple numbered rows) | F-12 | medium |
| R6 | Normalise property-child order in the finaliser instead of failing on Pandoc-originated order | F-08 | low |
| R7 | Ship or require a CSL matching biblatex `style=`; warn on mismatch | F-11 | low |
| R8 | Per-occurrence reference invariant (not per label); equation-count invariant source ↔ report; report probe timeouts as a tool error, not UNSUPPORTED | F-04, F-05 limits | low |
| R9 | Property-based tests for `l2w_math.lua` (random rows/cells → linearised OMML) | F-01 class | low |
| R10 | Make the 3 platform tests portable | F-09 | trivial |
