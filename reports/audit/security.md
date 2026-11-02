# Security and robustness

Threat model: the LaTeX project (and any SVG/PDF/bib in it) may be hostile. The configuration file, profiles,
the reference template and the installed tools are trusted (they are chosen by the person running the tool).
Testing happened only inside the session scratch directory, with a canary file standing in for a secret.

| Area | Result | Status | Evidence |
|---|---|---|---|
| Shell injection | argv vectors only, `shell=False`, stdin closed, mandatory timeout | VERIFIED static | `process.py` |
| LaTeX shell escape | `-no-shell-escape`, `-interaction=nonstopmode`, `-halt-on-error` | VERIFIED static; runtime BLOCKED (no TeX) | `tikz_render.py:22` |
| `\write18`, `\immediate\write`, `\openin` in the body | pinned as explicit errors by the corpus (`test_corpus.py`) | not re-run under 3.12 (BLOCKED) | corpus cases `write18-shell-escape`, `openin`, `immediate-write-file` |
| Arbitrary file read through Pandoc's reader | **leak found and fixed** (F-02) | VERIFIED before/after | `findings.md` F-02, `test_f02_*` |
| Arbitrary file read through TeX during TikZ rendering | `openin_any` not restricted | BLOCKED (no TeX), F-07 | static |
| Literal outside-root / symlink escape (`\input`, `\include`, `\usepackage`, graphics, staging) | refused | VERIFIED (literal include/usepackage); static (symlinks: `source_graph.py:306`, `staging.py:96`) | |
| Directory traversal in staging | absolute, `..`, drive-letter paths refused; destination re-checked inside staging dir | VERIFIED static; existing tests pass | `staging.py:77-155` |
| Deletion safety | ownership marker bound to the path; links, root, home, project and its parents refused | VERIFIED static; existing tests pass | `workspace.py:140-239` |
| XML external entities (OOXML) | lxml with `resolve_entities=False`, `no_network`, no DTD, no huge trees | VERIFIED static | `ooxml/package.py:29-35` |
| XML entities (SVG) | `<!ENTITY` refused before parsing; stdlib expat 2.9.0 (amplification limits) | VERIFIED static | `svg_validate.py:207-215` |
| ZIP bombs / malformed DOCX | size, entry-count and ratio limits when inspecting packages | VERIFIED static | `docx_package.py:115-180` |
| Duplicate ZIP entries | refused on load | VERIFIED static | `ooxml/package.py:68-71` |
| Output overwrite | `--output` not confined (F-06) | static | |
| Resource exhaustion | external tools time out; scanner has `MAX_INCLUDE_DEPTH` and cycle detection; a timed-out `latex` kills only the direct child (`subprocess.run`), not grandchildren | static | `process.py`, `source_graph.py` |
| Supply chain | three runtime deps, lower bounds only, no lock file, no hashes; external binaries never downloaded by the tool | static | `pyproject.toml` |
| Dependency CVEs | not scanned (no advisory database reachable from the container) | NOT TESTED | |

## The F-02 leak in detail

Before the fix, each of these reached `preliminary.docx` with the canary text or image bytes:

| Construct | Diagnostic before | After |
|---|---|---|
| `\input{\macro}` → absolute path | warning `dynamic-path` | error, Pandoc never runs |
| `\includegraphics{\macro}` → outside PNG | warning (image embedded, SHA-256 identical) | error |
| `\let\inp\input` / `\let\ig\includegraphics` | warning `unsupported-syntax` | error |
| `\lstinputlisting{/abs}` | warning | error |
| `\inputminted{x}{/abs}` | **none** | error |
| `\subfile{/abs}` | error (text already in the workspace DOCX) | error, Pandoc never runs |
| wrapper `\newcommand{\inp}[1]{\input{#1}}` | warning | error (the `#1` makes the path dynamic) |

Not affected (tested): literal `\include{/abs}` and `\usepackage{/abs}` (already errors); `\verbatiminput`,
`\import`, `\InputIfFileExists`, `\csname input\endcsname`, `\loadglsentries` (Pandoc 3.1.3 does not read them).

**Residual risk.** This is a deny-list on a scanner that does not expand macros. The structural fix (roadmap R3)
is to give Pandoc only one flattened file and run it with `--sandbox`, so the reader cannot open anything else.
Until then, treat untrusted projects as a residual risk.

Filenames with spaces, Unicode, long paths: covered by existing tests (`test_platform_paths.py`, `test_staging.py`).
Those pass except the three Linux-portability test bugs (F-09).
