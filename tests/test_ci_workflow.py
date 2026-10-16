"""The CI workflow discovers tests and frontend scripts from the tree."""

import os
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> str:
    return WORKFLOW.read_text()


def test_workflow_loops_over_every_test_file():
    text = _workflow()
    assert "for f in tests/test_*.py; do" in text
    named = re.findall(r"uv run python (tests/test_[A-Za-z0-9_]+\.py)", text)
    assert named == []
    on_disk = sorted(p.name for p in (ROOT / "tests").glob("test_*.py"))
    assert "test_ci_workflow.py" in on_disk
    assert len(on_disk) >= 15


def test_workflow_checks_frontend_scripts_by_glob():
    text = _workflow()
    assert "for f in frontend/static/*.js; do" in text
    named = re.findall(r"node --check (frontend/static/[A-Za-z0-9_-]+\.js)", text)
    assert named == []
    scripts = sorted(p.name for p in (ROOT / "frontend" / "static").glob("*.js"))
    assert "waterfall.js" in scripts
    assert "radio-audio-worklet.js" in scripts
    assert not any(name.startswith("vendor") for name in scripts)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
