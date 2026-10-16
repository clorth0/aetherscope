"""CI fails when a backend module is missing from the architecture page."""

import os
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check_architecture_names.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def test_current_tree_names_every_module():
    result = _run()
    assert result.returncode == 0, result.stdout


def test_gps_commit_is_missing_from_the_page():
    result = _run("--rev", "55d616e")
    assert result.returncode == 1
    assert "gps" in result.stdout


def test_workflow_runs_the_check():
    text = WORKFLOW.read_text()
    assert "scripts/check_architecture_names.py" in text


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
