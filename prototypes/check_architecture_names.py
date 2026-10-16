#!/usr/bin/env python3
"""PROTOTYPE. Not wired into CI.

Every backend module stem must appear in docs/architecture.md. Pass a
git revision to measure an old tree. Exit 1 when a module is missing.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def modules_at(rev: str | None) -> list[str]:
    if rev is None:
        names = []
        for path in sorted((ROOT / "backend").glob("*.py")):
            if path.stem != "__init__":
                names.append(path.stem)
        return names
    out = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", rev], text=True)
    names = []
    for line in out.splitlines():
        if line.startswith("backend/") and line.endswith(".py") and not line.endswith("__init__.py"):
            names.append(Path(line).stem)
    return names


def architecture_at(rev: str | None) -> str:
    if rev is None:
        return (ROOT / "docs" / "architecture.md").read_text()
    return subprocess.check_output(["git", "show", f"{rev}:docs/architecture.md"], text=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rev", default=None)
    args = parser.parse_args()
    missing = [name for name in modules_at(args.rev) if name not in architecture_at(args.rev)]
    label = args.rev or "HEAD"
    if missing:
        print(f"{label} missing from docs/architecture.md: {' '.join(missing)}")
        return 1
    print(f"{label}: every backend module is named in docs/architecture.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
