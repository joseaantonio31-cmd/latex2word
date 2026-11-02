# Reproducibility

## Environment

Recorded in `logs/environment.txt` and `logs/doctor.txt`: Linux x86_64, Python 3.13.18, lxml 6.1.3 / libxml2 2.14.6,
PyYAML 6.0.3, jsonschema 4.26.0, pytest 9.2.0, Pandoc 3.1.3. **Absent**: Pandoc 3.12 (certified; installing the
upstream binary was refused by the session permission policy), TeX, dvisvgm, rsvg-convert, Inkscape, Microsoft Word.

## Commits

| SHA | Content |
|---|---|
| `35547d4c516c7bcfd65530686bd46b18e703c5ea` | audit start (= `origin/main`) |
| `2d37916` | F-01..F-04 fixes, regression tests, oracle, clean fixture, harness |
| `a61debb` | F-05 fix |
| (next commit) | these reports |

No original fixture was modified. New fixtures live in `tests/fixtures/audit/`. The only change to an existing test
is the marked assertion in `tests/test_source_graph.py` (F-02).

## Commands

```bash
python -m pip install -e ".[dev]"
```

```bash
python -m pytest -q -p no:cacheprovider tests/test_audit_regressions.py tests/test_audit_oracle.py tests/test_audit_e2e.py
```

```bash
python -m pytest -q -p no:cacheprovider
```

```bash
python tools/audit/ooxml_oracle.py path/to/output.docx
```

```bash
python tools/audit/harness_build.py project.yaml
```

The harness only exists to get finaliser evidence on an uncertified Pandoc. It disables the schema-order check and
the F-05 gate. Its results are never evidence of a certified build.

## Determinism

VERIFIED (harness, Pandoc 3.1.3): two builds of `clean_article` in different workspace and output directories give
identical `preliminary.docx` (`8c00242c…eb912`) and `main.docx` (`e6fa760b…d338d2`). The tool sets
`SOURCE_DATE_EPOCH=0` for Pandoc and TeX. Determinism of figure rendering is BLOCKED (no TeX).

## What a reproducer on the certified toolchain should do

1. Install Pandoc 3.12, TeX Live or MiKTeX with dvisvgm, and run `latex2word doctor` (expect READY).
2. Run the full suite. Expected: all pass except the 3 Linux-portability tests (F-09). This is the gate this audit
   could not run.
3. Run `tests/test_audit_e2e.py`. With 3.12 it uses no bypass.
4. Run the integration suite against the original thesis (`-m integration`, env variables in
   `tests/test_integration_reference.py`) to settle F-10.
5. Open the output in Word, update fields (F9), and walk through `visual_review.md`.
