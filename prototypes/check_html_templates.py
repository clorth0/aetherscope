#!/usr/bin/env python3
"""PROTOTYPE. Not wired into CI.

Flag template literals that contain an HTML tag and interpolate a value
the template does not pass through escapeHtml or encodeURIComponent.

An assignment-only scan of `innerHTML =` misses the historical bugs.
Those templates are built in helpers and joined into innerHTML later.
Exit 1 when any such template remains.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "frontend" / "static" / "waterfall.js"


def load(spec: str) -> str:
    if spec.startswith("git:"):
        rev, path = spec[4:].split(":", 1)
        return subprocess.check_output(["git", "show", f"{rev}:{path}"], text=True)
    return Path(spec).read_text()


def templates(src: str) -> list[tuple[int, str]]:
    found = []
    i = 0
    while True:
        start = src.find("`", i)
        if start < 0:
            break
        j = start + 1
        while j < len(src):
            if src[j] == "\\":
                j += 2
                continue
            if src[j] == "`":
                break
            j += 1
        body = src[start + 1 : j]
        if "<" in body and "${" in body:
            found.append((src.count("\n", 0, start) + 1, body))
        i = j + 1
    return found


def raw_exprs(body: str) -> list[str]:
    raw = []
    for expr in re.findall(r"\$\{([^{}]*)\}", body):
        text = " ".join(expr.split())
        if text.startswith("escapeHtml(") or text.startswith("encodeURIComponent("):
            continue
        raw.append(text)
    return raw


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "spec",
        nargs="?",
        default=str(DEFAULT),
        help="a file path, or git:<rev>:<path>",
    )
    args = parser.parse_args()
    hits = []
    for line, body in templates(load(args.spec)):
        exprs = raw_exprs(body)
        if exprs:
            hits.append((line, exprs))
    print(f"{args.spec}: {len(hits)} HTML templates interpolate a raw value")
    for line, exprs in hits:
        shown = ", ".join(exprs[:4])
        print(f"  L{line}: {shown}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
