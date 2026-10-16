"""The html tag escapes values, and CI rejects an untagged HTML template."""

import os
import subprocess
import sys
import tempfile
import textwrap
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check_html_templates.py"
WATERFALL = ROOT / "frontend" / "static" / "waterfall.js"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _run_checker(spec: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), spec],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def test_current_waterfall_is_tagged():
    result = _run_checker(str(WATERFALL))
    assert result.returncode == 0, result.stdout


def test_historical_waterfall_is_rejected():
    result = _run_checker("git:b76ef78^:frontend/static/waterfall.js")
    assert result.returncode == 1
    assert "HTML templates are not html" in result.stdout


def test_checker_ignores_a_comparison_and_requires_the_tag():
    unsafe = "const row = `<div>${name}</div>`;\n"
    tagged = "const row = html`<div>${name}</div>`;\n"
    comparison = "const label = `Signal: ${pct < 25 ? 'weak' : 'ok'}`;\n"
    with tempfile.TemporaryDirectory() as tmp:
        unsafe_path = Path(tmp) / "unsafe.js"
        tagged_path = Path(tmp) / "tagged.js"
        comparison_path = Path(tmp) / "comparison.js"
        unsafe_path.write_text(unsafe)
        tagged_path.write_text(tagged)
        comparison_path.write_text(comparison)
        assert _run_checker(str(unsafe_path)).returncode == 1
        assert _run_checker(str(tagged_path)).returncode == 0
        assert _run_checker(str(comparison_path)).returncode == 0


def test_tag_escapes_text_and_keeps_raw_markup():
    src = WATERFALL.read_text()
    start = src.index("function escapeHtml")
    end = src.index("function summarizeFields")
    script = src[start:end] + textwrap.dedent("""
        const payload = '<img src=x onerror=alert(1)>';
        const safe = html`<span class="event-model">${payload}</span>`;
        if (safe.includes("<img")) {
          console.error(safe);
          process.exit(1);
        }
        if (!safe.includes("&lt;img")) process.exit(1);
        const nested = html`<div>${raw(html`<b>${payload}</b>`)}</div>`;
        if (nested.includes("<img") || !nested.includes("<b>")) process.exit(1);
        const once = html`<span>${"a & b"}</span>`;
        if (once.includes("&amp;amp;") || !once.includes("a &amp; b")) process.exit(1);
        const attr = html`<button data-id="${'a"b'}">x</button>`;
        if (attr.includes('a"b') || !attr.includes("&quot;")) process.exit(1);
    """)
    result = subprocess.run(["node", "--input-type=commonjs", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_call_sites_do_not_escape_before_the_tag():
    text = WATERFALL.read_text()
    assert text.count("escapeHtml(") == 2
    workflow = WORKFLOW.read_text()
    assert "scripts/check_html_templates.py" in workflow
    assert "fetch-depth: 0" in workflow


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
