#!/usr/bin/env python3
"""Fail when an HTML template literal is not the html`` tag.

The tag escapes every interpolated value. A plain template that still
contains a tag is the bug this check exists for. `<` inside ${} is a
comparison, not markup, so it does not count.
"""

from __future__ import annotations

import argparse
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


def templates(src: str) -> list[tuple[int, str, bool]]:
    found = []
    i = 0
    while i < len(src):
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
        found.append((src.count("\n", 0, start) + 1, body, _html_tagged(src, start)))
        i = j + 1
    return found


def _html_tagged(src: str, backtick: int) -> bool:
    i = backtick - 1
    while i >= 0 and src[i] in " \t\r\n":
        i -= 1
    return src[max(0, i - 3) : i + 1] == "html"


def _outside_interpolations(body: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(body):
        if body.startswith("${", i):
            depth = 1
            i += 2
            while i < len(body) and depth:
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                i += 1
            continue
        out.append(body[i])
        i += 1
    return "".join(out)


def hits(src: str) -> list[int]:
    bad = []
    for line, body, tagged in templates(src):
        if tagged or "<" not in _outside_interpolations(body):
            continue
        bad.append(line)
    return bad


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec", nargs="?", default=str(DEFAULT))
    args = parser.parse_args()
    found = hits(load(args.spec))
    print(f"{args.spec}: {len(found)} HTML templates are not html`` tagged")
    for line in found:
        print(f"  L{line}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
