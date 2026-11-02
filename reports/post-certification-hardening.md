# Post-certification hardening

Branch `hardening/post-certification`, from `main` `159bae4` (which contains the README banner and documentation
commits). Windows 11 Home, Python 3.14.7, MiKTeX 26.5 (pdfTeX 4.27), dvisvgm 3.6, Microsoft Word 16.0, Pandoc 3.12
(certified) and 3.13 (`TESTED_UNCERTIFIED`). Private evidence (logs, the private validation document, Word renders)
is kept outside the repository.

| Commit | Change |
|---|---|
| `d025e27` | fix(docx): remove local CSL path from document metadata |
| `7e4c060` | fix(pagination): skip bookmark boundaries after section breaks |
| `153aad5` | docs: document TikZ macros from local style files |
| `9df78b3` | feat(security): make the confined Pandoc reader the default |

## Phase A — post-certification fixes

### CSL path in DOCX metadata

* **Reproduction.** A build whose CSL file lay in a directory with a unique marker; every part of the output, final
  and preliminary DOCX was searched (UTF-8 and UTF-16). The marker appeared in exactly one place:
  `docProps/custom.xml`, `<property name="csl"><vt:lpwstr>` (absolute CSL path), written by Pandoc for the `csl`
  metadata. No other part held a local path. The private validation document showed the same property.
* **Fix.** The finaliser (`ooxml/custom_properties.py`) removes the custom properties named in
  `BUILD_PATH_PROPERTIES = {"csl"}` after Pandoc has rendered the bibliography. Other properties keep name, pid and
  value; the part is rewritten only when a property is removed.
* **Tests** (`tests/test_docx_privacy.py`): only `csl` is removed, look-alike user properties stay; no trace of the
  marked path in any part; an A/B build with the removal disabled differs only in `docProps/custom.xml`, by that
  property (bibliography text unchanged); a build without a CSL is byte-identical.
* **Validation document:** every part byte-identical to the certified DOCX except `docProps/custom.xml` (the `csl`
  property gone, `bibliography` kept); no local path or `.csl` reference left in the package.

### Bookmark boundaries after section breaks

* **Reproduction.** A report with a labelled front-matter chapter, `\pagenumbering{roman}` then `{arabic}`: with
  Pandoc 3.12 the section-break paragraph is followed by `bookmarkEnd`, `bookmarkStart`, then the chapter heading,
  which did not receive `w:pageBreakBefore w:val="0"`. Pandoc 3.13 writes no body-level bookmarks there.
* **Fix.** `_next_paragraph` treats `bookmarkStart` and `bookmarkEnd` as transparent; any other element (table,
  content control) still ends the search.
* **Tests** (`tests/test_pagination_bookmarks.py`): each boundary alone, both orders, several adjacent, unrelated
  elements not skipped, `apply_pagination` on the 3.12 shape, an end-to-end build. With the old semantics the six
  `bookmarkEnd` cases fail, including the 3.12 build.
* **Validation document (3.12):** `word/document.xml` gains exactly one `pageBreakBefore w:val="0"` (on the first
  chapter), matching 3.13; nothing else changes.

### Documentation

`docs/known-limitations.md` and `docs/supported-latex.md`: macros a TikZ/circuitikz/pgfplots drawing uses must be
defined in the main preamble; a macro defined only in a local `.sty` makes the figure fail (`L2W-E-FIG-001`); text-mode
use of local `.sty` macros is unaffected.

### Phase A gate

| | Pandoc 3.13 | Pandoc 3.12 |
|---|---|---|
| full suite | 1,275 passed, 0 failed | 1,275 passed, 0 failed |
| private integration | 24/24 | 24/24 |
| validation document: regression guard vs frozen reference (1,405 OMML, 54 display, 23 drawings, 21 figure / 31 table captions, 47 notes, 74 bibliography entries, 486/486 references, 3 sections, PAGE field, 4 contents fields, all paragraph and note texts) | all identical | all identical |
| Word: opens, fields updated, PDF exported | 163 pages, statistics identical | 163 pages, statistics identical |
| rendered pages differing from the frozen reference | 0 / 163 | 0 / 163 |
| source files | 87/87 unchanged | 87/87 unchanged |

## Phase B — the confined reader is the default

### Review before the switch

* The unknown-construct probe never reads the project: it runs Pandoc on a synthetic document of command names
  with constant arguments in latex2word's own directory. The compatibility probe uses fixed inputs. The confined
  reader is the only place where user TeX reaches Pandoc's LaTeX reader.
* Everything the flattener refuses is already a source-graph error in the single-pass path (brace-less `\input`,
  `\subfile`); `\input` inside a macro body is inlined (equivalent to Pandoc's expansion); dynamic paths, symlink
  escapes, absolute paths and `..` stop at the graph gate before any reader runs; a repeated local package is inlined
  twice (harmless).
* Gap found and closed: the profile's `pandoc.extra_args` reached only the writer run, so a reader option would have
  stopped working silently. They now also reach the sandboxed reader (one that needs a file is refused and fails).

### Design

* `run_build` always uses the confined reader; `LATEX2WORD_EXPERIMENTAL_CONFINED_READER` is retired (no effect).
* `LATEX2WORD_UNSAFE_LEGACY_READER=1` restores the single unconfined run for diagnosis only; every such build
  carries warning `L2W-W-CONFINE-005` (so it also fails a `fail_on: warning` configuration).
* The audit tools run the default with `python -m latex2word build --json` and no reader variable; the legacy reader
  is the liveness baseline; `--harness` keeps the gate-bypassing runner for an uncertified Pandoc.

### Evidence

| | Pandoc 3.13 | Pandoc 3.12 |
|---|---|---|
| `doctor` | READY, `TESTED_UNCERTIFIED` | READY, `SUPPORTED` |
| full suite | 1,281 passed, 0 failed | 1,281 passed, 0 failed |
| hardening groups | 152 passed, 2 symlink skips | 152 passed, 2 symlink skips |
| attack suite, default command | no leak in any of 14 vectors | no leak in any of 14 vectors |
| `filecontents` (live) | default: `L2W-E-CONFINE-001`, canary absent from AST, preliminary and final DOCX; legacy reader: canary in preliminary and final DOCX | same |
| default vs legacy on 7 fixtures | identical `document.xml` and diagnostics | identical |
| private integration (default) | 24/24 | 24/24 |
| validation document (default) | byte-identical to Phase A (confined AST written) | byte-identical to Phase A |
| Word | 163 pages, 0 / 163 pages differ | 163 pages, 0 / 163 pages differ |
| source files | 87/87 unchanged | 87/87 unchanged |

The 12 other live vectors stop earlier at the source graph (`dynamic-path`, `unsupported-syntax`,
`unsupported-command`, `outside-root`); `\iffalse` nesting is not followed by 3.12/3.13 in either path.

### Security contract

README, SECURITY.md, known-limitations and architecture now distinguish **Pandoc reader confinement (default)** from
**TeX figure-renderer isolation (not done, F-07)**. Trusted input remains required.

## Phase C — F-07 TeX isolation (investigation)

### What the figure process needs

* `latex`: read the generated figure `.tex`, the staged project files a drawing uses (pgfplots data), the TeX
  distribution (formats, packages, fonts, configuration); write DVI, log and aux into the render directory.
* `dvisvgm`: read the DVI and the distribution's fonts; write the SVG into the render directory.
* Neither needs the user's other files, other projects, network or any write outside the render directory.

### Mechanisms on this machine (Windows 11 Home, no administrator)

| Mechanism | Finding |
|---|---|
| Windows Sandbox | Pro/Enterprise only; not present |
| Docker, WSL | not installed (not installed for this) |
| Low-integrity token, job object | do not restrict reads: no file-system boundary |
| **AppContainer** (no capabilities) | available without administrator rights; only objects whose ACL grants the container (or system areas) are readable; no network |

### Prototype (outside the repository)

An AppContainer launcher (`CreateAppContainerProfile` + `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`) ran `latex`
and `dvisvgm` with read access granted to the MiKTeX roots and the staged project and write access to the render
directory only; every grant was reverted afterwards (verified: no container entry left). Findings on the way:
AppContainers redirect AppData, so MiKTeX must be pointed at its roots (`MIKTEX_USERCONFIG`, `MIKTEX_USERDATA`,
`MIKTEX_USERINSTALL`), and MiKTeX keeps its setup state in `HKCU\Software\MiKTeX.org`, which needs a read grant.

| Attack on an outside canary | Unisolated control | Isolated |
|---|---|---|
| `\openin` + `\read` | read | refused (TeX ran, reported no file) |
| `\IfFileExists` + `\input` | read | refused |
| indirectly built path (`\edef`) + `\input` | read | refused |
| pgfplots `\pgfplotstableread` | read | refused ("Could not read table file") |
| `\openout` outside the render directory | not live (MiKTeX refuses absolute-path writes by default) | — |

Legitimate figures under isolation: TikZ (positioning, styles), circuitikz and pgfplots with an external CSV file
and an expression rendered through `latex` and `dvisvgm --no-fonts` to SVGs byte-identical to the unisolated renders.

### Verdict: `F07_ISOLATION_NEEDS_MORE_WORK`

The OS boundary works, but it is not ready: (1) it needs read grants on the user's MiKTeX installation and on its
registry key, a machine-changing setup step that is a product decision; (2) that registry grant exposes MiKTeX's
package-manager token to TeX; (3) on-the-fly package installation and format updates cannot run inside the
container; (4) it is not integrated into `tikz_render`, has no suite tests, and is untested with TeX Live and on
other platforms. Nothing is enabled; the trusted-input requirement stays.

## Remaining

* F-07: the four points above.
* Pandoc 3.13 certification remains an owner decision.
* F-04 per-occurrence markers do not cover `Vref`, `labelcref`, `crefrange`, `cpageref`.
