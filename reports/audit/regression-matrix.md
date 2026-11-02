# Regression matrix

E = executed in the audit container (Pandoc 3.1.3). "Before" = result on `35547d4` (unfixed source, new tests run
against it); "After" = on the fix commits.

| Test | Requirement | Failure mode guarded | Oracle | Kind | Before | After |
|---|---|---|---|---|---|---|
| `test_audit_regressions::test_f01_bracket_starting_an_aligned_row_survives_in_the_omml` | math content preserved | row content deleted | independent OMML linearisation incl. `m:begChr` | integration (Pandoc) | FAIL | PASS |
| `::test_f01_spacing_argument_directly_after_the_row_break_is_still_spacing` | `\\[2pt]` is spacing | fix over-corrects into content | OMML | integration | PASS (control) | PASS |
| `::test_f01_bracket_after_starred_row_break_survives` | `\\* [c]` | same as F-01 | OMML | integration | FAIL | PASS |
| `::test_f01_alternating_user_array_keeps_kernel_semantics` | kernel `array` semantics | over-correction | OMML | integration | PASS (control) | PASS |
| `::test_f01_numbered_align_keeps_the_bracket_row_and_its_number` | numbered align, 3.12-shaped input | deletion + number kept → looks valid | module output | unit (`pandoc lua`) | FAIL | PASS |
| `::test_f01_eqnarray_is_a_kernel_environment…` | eqnarray semantics | over-correction | module output | unit | PASS (control) | PASS |
| `::test_f03_label_with_nested_braces…` | label stripped and registered | TeX left in equation, label lost | module output | unit | FAIL | PASS |
| `::test_f02_constructs_pandoc_opens_from_any_path_are_errors` ×9 | containment of reads | file outside the root embedded in the DOCX | source-graph severity (the build stops on any ERROR, verified by real builds) | unit | 9 FAIL | 9 PASS |
| `::test_f02_literal_paths_outside_the_root_were_already_errors` | literal containment | regression of existing check | severity | unit | PASS (control) | PASS |
| `::test_f02_commands_pandoc_does_not_open_stay_warnings` | no over-blocking | needless build failures | severity | unit | PASS (control) | PASS |
| `::test_f04_*` ×3 | references reach the document | silent reference loss | hand-built scan + report | unit | 3 FAIL | 3 PASS |
| `::test_f05_*` ×2 | build honours Pandoc classification | silent use of an unsupported Pandoc | injected assessment | unit | 2 FAIL | 2 PASS |
| `test_audit_oracle::*` ×9 | the oracle detects faults | blind oracle | fault injection | meta | n/a (new tool) | PASS |
| `test_audit_e2e::test_build_is_byte_reproducible…` | determinism, package integrity | non-reproducible output, broken package | SHA-256, oracle | e2e (harness bypasses under 3.1.3) | n/a | PASS |
| `test_audit_e2e::test_math_structure_matches_the_source` | OMML structure | wrong/flattened math | element counts from source | e2e | n/a | PASS |
| `test_audit_e2e::test_text_escapes_and_accents_survive` | text fidelity | lost escapes/Unicode | literal strings | e2e | n/a | PASS |

Before/after logs: `logs/regressions-before-fix.log` (16 failed, 5 passed; the F-05 tests were added later and
checked separately: 2 failed on the pre-F-05 `build.py`), `logs/regressions-after-fix.log`.

## Whole suite (E, Pandoc 3.1.3)

| Revision | Passed | Failed | Errors | Skipped | Log |
|---|---|---|---|---|---|
| `35547d4` baseline | 1012 | 92 | 18 | 9 | `logs/baseline-pytest-pandoc-3.1.3.log` |
| `2d37916` (F-01..F-04) | 1033 + new | 92 (identical set) | 18 | 9 | (re-run during audit) |
| `a61debb` (F-05) | 986 | 141 | 30 | 9 | `logs/after-fix-pytest-pandoc-3.1.3.log` |

Baseline failure causes (classified): Pandoc 3.1.3 AST differences (equation wrapper stripped, `\Cref` dropped,
`\autoref` typed `autoref`, widths not converted to %), missing `rsvg-convert` for SVG, the schema-order error of F-08,
and 3 non-portable tests (F-09). After F-05, the 73 tests that newly fail all stop at `L2W-E-PANDOC-003`
(`logs/after-fix-new-failures.txt`).

**Not executable here**: on Pandoc 3.12 the expectation is "all pass except F-09's 3", as the README states. This
is **BLOCKED**, so no claim is made that the fixes keep the certified suite green.

## Metamorphic and property-style checks performed

* Workspace path changed between two builds → identical output bytes (determinism).
* `\\[2pt]` vs `\\ [2pt]` inside `aligned` must differ (spacing vs content); inside `array` they must not.
* Each containment vector tried in literal, macro, wrapper-macro and `\let`-alias form.
* Not done: generated property-based tests (no `hypothesis` installed). Recommended for `l2w_math.lua`
  (random cells/rows → linearised OMML must contain every cell token).
