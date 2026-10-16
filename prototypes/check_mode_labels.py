#!/usr/bin/env python3
"""PROTOTYPE. Not wired into CI.

The server stores a mode string in several places. The status pill names
a different set. Modes the pill does not name fall through to Idle.
Exit 1 when those sets differ.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def assigned_modes(app: str) -> set[str]:
    return set(re.findall(r'_state\["mode"\]\s*=\s*"([^"]+)"', app))


def status_modes(js: str) -> set[str]:
    match = re.search(r"function refreshStatusUI\(\) \{([\s\S]*?)\n\}", js)
    if not match:
        raise SystemExit("refreshStatusUI not found")
    return set(re.findall(r'serverMode === "([^"]+)"', match.group(1)))


def slot_tuples(app: str) -> list[str]:
    return [" ".join(body.split()) for body in re.findall(r"for slot in \(([^)]*)\)", app)]


def main() -> int:
    app = (ROOT / "backend" / "app.py").read_text()
    js = (ROOT / "frontend" / "static" / "waterfall.js").read_text()
    assigned = assigned_modes(app)
    named = status_modes(js)
    idle_gap = sorted(assigned - named - {"idle"})
    unknown = sorted(named - assigned)
    slots = slot_tuples(app)
    print("server mode assignments:", " ".join(sorted(assigned)))
    print("status pill names:", " ".join(sorted(named)))
    print("pill text falls through to Idle:", " ".join(idle_gap) or "(none)")
    print("pill names with no server assignment:", " ".join(unknown) or "(none)")
    print(f"copied job-slot tuples: {len(slots)}")
    if len(slots) > 1 and len(set(slots)) == 1:
        print("the slot tuples are identical copies")
    return 1 if idle_gap or unknown or len(slots) != 1 else 0


if __name__ == "__main__":
    sys.exit(main())
