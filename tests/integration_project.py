"""Expectations about the external reference project of the integration tests, kept out of version control.

The integration tests (``-m integration``) run latex2word on a real LaTeX project named by
``LATEX2WORD_INTEGRATION_PAPER_ROOT``. The tracked tests only check generic properties (the build succeeds, the
source tree is untouched, figures are vectors, numbering is consecutive, profile geometry...). Everything specific
to that project (figure names and kinds, figure patches, numbering, section labels, counts) comes from a local JSON
file named by ``LATEX2WORD_INTEGRATION_EXPECTATIONS``; without it the tests skip. Keep the file outside the
repository (``*.local.json`` files are ignored if one is kept inside). Its keys are documented in
``docs/development.md``.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

ENVIRONMENT_VARIABLE = "LATEX2WORD_INTEGRATION_EXPECTATIONS"


@lru_cache(maxsize=1)
def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def expectations() -> dict[str, Any]:
    """The project-specific expectations; skips the calling test when they are not provided."""
    value = os.environ.get(ENVIRONMENT_VARIABLE)
    if not value or not Path(value).is_file():
        pytest.skip(f"{ENVIRONMENT_VARIABLE} is not set to an existing JSON file")
    return _load(value)


def expect(*keys: str) -> Any:
    """One value of the expectations, e.g. ``expect("counts", "math_inline")``; a missing key fails loudly."""
    node: Any = expectations()
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            pytest.fail(f"{ENVIRONMENT_VARIABLE}: missing key {'.'.join(keys)}")
        node = node[key]
    return node
