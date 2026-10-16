#!/usr/bin/env python3
"""PROTOTYPE. Not wired into CI.

Compare tests/test_*.py with the filenames listed in the GitHub Actions
workflow. Exit 1 when the workflow names a subset. Also show that
`python tests/test_*.py` executes only the first file, and that unittest
discovery does not collect this suite's function-style tests.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
TESTS = ROOT / "tests"


def test_files() -> list[Path]:
    return sorted(TESTS.glob("test_*.py"))


def ci_named(files: list[Path]) -> list[str]:
    text = CI.read_text()
    return [p.name for p in files if p.name in text]


def function_tests(path: Path) -> int:
    tree = ast.parse(path.read_text())
    return sum(
        1
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )


def unittest_cases(path: Path) -> int:
    tree = ast.parse(path.read_text())
    count = 0
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            name = base.id if isinstance(base, ast.Name) else ""
            attr = base.attr if isinstance(base, ast.Attribute) else ""
            if name == "TestCase" or attr == "TestCase":
                count += sum(
                    1
                    for item in node.body
                    if isinstance(item, ast.FunctionDef) and item.name.startswith("test_")
                )
    return count


def main() -> int:
    files = test_files()
    named = ci_named(files)
    missing = [p.name for p in files if p.name not in named]
    fn = sum(function_tests(p) for p in files)
    cases = sum(unittest_cases(p) for p in files)
    in_ci_fn = sum(function_tests(TESTS / name) for name in named)
    print(f"test files on disk: {len(files)}")
    print(f"test files named by .github/workflows/ci.yml: {len(named)}")
    print(f"function-style tests: {fn}")
    print(f"unittest.TestCase methods: {cases}")
    print(f"function-style tests in the CI list: {in_ci_fn}")
    print(f"function-style tests CI does not run: {fn - in_ci_fn}")
    if missing:
        print("missing from CI:")
        for name in missing:
            print(f"  {name}")
    print(
        "python tests/test_*.py is not a suite runner. "
        "The interpreter executes the first path and puts the rest in argv."
    )
    return 1 if missing or cases == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
