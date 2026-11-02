"""Architecture rules of the layout engine: generic code, publisher data in profiles and registered adapters only."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from conftest import REPO_ROOT

SRC = REPO_ROOT / "src" / "latex2word"
PUBLISHER_ADAPTERS = {SRC / "metadata" / "ieee.py"}
BRANCH_ON_IDENTITY = re.compile(r"(?:profile(?:_id)?|\.id|document_?class|docclass)\s*(?:==|!=)\s*[\"']")


def code_constants(path: Path) -> list[str]:
    """String constants and identifiers of the module, excluding docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            found.append(node.value)
        elif isinstance(node, ast.Name):
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            found.append(node.attr)
    return found


def test_generic_modules_contain_no_publisher_specific_code_and_no_identity_branches():
    modules = sorted(SRC.rglob("*.py"))
    assert len(modules) > 30
    publisher_code = [
        p.relative_to(SRC).as_posix() for p in modules if p not in PUBLISHER_ADAPTERS
        if any(re.search(r"ieee", c, re.I) for c in code_constants(p))
    ]
    branches = [p.relative_to(SRC).as_posix() for p in modules if BRANCH_ON_IDENTITY.search(p.read_text(encoding="utf-8"))]
    assert publisher_code == [] and branches == []


def test_the_front_matter_marker_never_appears_in_production_files():
    marker = "IEEE_FRONTMATTER_END"
    scanned = [p for p in (REPO_ROOT / "src").rglob("*") if p.is_file() and p.suffix in {".py", ".yaml", ".lua", ".json"}]
    assert scanned and not [p for p in scanned if marker in p.read_text(encoding="utf-8", errors="ignore")]
