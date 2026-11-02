"""Audit harness: run ``latex2word build`` with ONE validator check disabled.

Purpose: under Pandoc 3.1.3 (the only Pandoc available in the audit container) every
build fails on ``L2W-E-LAYOUT-090`` because Pandoc 3.1.3's *own* default ``styles.xml``
has property children out of schema order (observed in ``preliminary.docx`` before
latex2word edits anything). That stops the pipeline before ``final.docx`` exists, so the
finaliser's output could not be inspected at all.

This harness disables only ``_check_schema_order`` so the remaining pipeline and every
other validator still run. Results obtained this way are labelled "harness" in the audit
reports and are NOT evidence of a passing build with the certified Pandoc 3.12.

Usage: ``python tools/audit/harness_build.py project.yaml [--output out.docx]``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import latex2word.ooxml.validation as validation
from latex2word.build import run_build
from latex2word.config import load_config


def main(argv: list[str]) -> int:
    validation._check_schema_order = lambda roots, result: None  # noqa: SLF001 (audit harness, see docstring)
    config = load_config(Path(argv[0]))
    output = Path(argv[argv.index("--output") + 1]) if "--output" in argv else None
    result = run_build(config, output=output)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
