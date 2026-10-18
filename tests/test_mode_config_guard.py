"""Guard: mode-config knowledge has one home, JS mirrors cannot drift.

Candidate #6 (Speculative) honoured with restraint: NO build step, NO
code generation, NO deriving JS from Python at runtime. The Python
dataclasses remain the authoritative field definitions, the per-mode
``aether-*.js`` read*/apply* functions remain hand-written mirrors,
and this static guard is the one home for the *correspondence*
between them. It fails when a field is added/renamed on one side
without the other, or when the demod vocabulary is written anywhere
except ``backend/config.py``.

Scope, honestly narrowed (see docs/architecture.md):
- Full mirrors (read == apply == dataclass, minus named exceptions):
  sweep, decode, capture, adsb, scan.
- Radio: apply-only mirror. Its read side is two inline
  ``start_radio`` emits ({demod, freq_mhz}); there are no radio gain
  or sample-rate inputs in the UI, so those dataclass fields are
  Python-only and keep their constructor defaults on the wire path.
- Scan-radio: not a dataclass-shaped wire payload at all. The JS
  emits {freqs, demod, squelch_dbfs}; the app handler renames
  ``freqs`` -> ``freqs_mhz``. Only demod + squelch are mirrored
  verbatim, so it is guarded as a named rename, not a full mirror.
- Decode ``label`` is Python-only: there is no decode-label input in
  the UI; the dataclass default/bound is the whole story.
"""

import ast
import os
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
STATIC = ROOT / "frontend" / "static"
INDEX = ROOT / "frontend" / "templates" / "index.html"

# mode -> (python file, dataclass, js file, read fn or None, apply fn, python-only fields)
MODES = {
    "sweep": ("sdr.py", "SweepConfig", "aether-sweep.js",
              "readSweepConfig", "applySweepConfigToInputs", set()),
    "decode": ("decoders.py", "DecodeConfig", "aether-decode.js",
               "readDecodeConfig", "applyDecodeConfigToInputs", {"label"}),
    "capture": ("capture.py", "CaptureConfig", "aether-capture.js",
                "readCaptureConfig", "applyCaptureConfigToInputs", set()),
    "adsb": ("adsb.py", "AdsbConfig", "aether-adsb.js",
             "readAdsbConfig", "applyAdsbConfigToInputs", set()),
    "radio": ("radio.py", "RadioConfig", "aether-radio.js",
              None, "applyRadioConfigToInputs",
              {"lna_gain", "vga_gain", "amp_enable", "sample_rate_hz"}),
    "scan": ("scan.py", "ScanConfig", "aether-scan.js",
             "readScanConfig", "applyScanConfigToInputs", set()),
}

SCAN_RADIO_PY_ONLY = {"lna_gain", "vga_gain", "amp_enable", "sample_rate_hz"}
SCAN_RADIO_WIRE_RENAME = {"freqs": "freqs_mhz"}


def _py_fields(rel_file, cls_name):
    tree = ast.parse((BACKEND / rel_file).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            return {s.target.id for s in node.body
                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
    raise AssertionError(f"{cls_name} not found in {rel_file}")


def _js_function_body(src, fn_name):
    m = re.search(r"function " + re.escape(fn_name) + r"\([^)]*\)\s*\{", src)
    assert m, f"{fn_name} not found"
    brace = src.index("{", m.start())
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
    raise AssertionError(f"unterminated {fn_name}")


def _js_read_fields(src, fn_name):
    body = _js_function_body(src, fn_name)
    m = re.search(r"return\s*\{", body)
    assert m, f"{fn_name} has no return object"
    brace = body.index("{", m.start())
    depth = 0
    for i in range(brace, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                obj = body[brace:i + 1]
                return set(re.findall(r"(?m)^\s*([A-Za-z_]\w*)\s*:", obj))
    raise AssertionError(f"unterminated return in {fn_name}")


def _js_apply_fields(src, fn_name):
    return set(re.findall(r"\bc\.([A-Za-z_]\w*)", _js_function_body(src, fn_name)))


def _js_emit_fields(src, event):
    fields = set()
    for m in re.finditer(r'socket\.emit\("' + re.escape(event) + r'",\s*\{', src):
        brace = src.index("{", m.start())
        depth = 0
        for i in range(brace, len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    obj = src[brace:i + 1]
                    fields |= set(re.findall(r"([A-Za-z_]\w*)\s*:", obj))
                    fields |= set(re.findall(r"[{,]\s*([A-Za-z_]\w*)\s*[,}]", obj))
                    break
    return fields


def test_python_js_field_correspondence_per_mode():
    for mode, (py_file, cls, js_file, read_fn, apply_fn, py_only) in MODES.items():
        py_fields = _py_fields(py_file, cls)
        src = (STATIC / js_file).read_text()
        mirrored = py_fields - py_only
        apply_fields = _js_apply_fields(src, apply_fn)
        assert apply_fields == mirrored, (
            f"{mode}: apply mirror {sorted(apply_fields)} != dataclass-mirrored "
            f"{sorted(mirrored)} (dataclass={sorted(py_fields)}, python-only={sorted(py_only)})")
        if read_fn is not None:
            read_fields = _js_read_fields(src, read_fn)
            assert read_fields == mirrored, (
                f"{mode}: read mirror {sorted(read_fields)} != dataclass-mirrored {sorted(mirrored)}")


def test_radio_read_side_is_the_inline_start_radio_emits():
    src = (STATIC / "aether-radio.js").read_text()
    py_fields = _py_fields("radio.py", "RadioConfig")
    mirrored = py_fields - MODES["radio"][5]
    assert _js_emit_fields(src, "start_radio") == mirrored


def test_scan_radio_wire_is_a_named_rename_not_a_mirror():
    src = (STATIC / "aether-radio.js").read_text()
    wire = _js_emit_fields(src, "start_scan_radio")
    renamed = {SCAN_RADIO_WIRE_RENAME.get(k, k) for k in wire}
    py_fields = _py_fields("radio.py", "ScanRadioConfig")
    assert renamed == py_fields - SCAN_RADIO_PY_ONLY, (
        f"scan_radio wire {sorted(wire)} -> {sorted(renamed)} != "
        f"{sorted(py_fields - SCAN_RADIO_PY_ONLY)}")


def test_status_config_keys_match_js_apply_registrations():
    app = (BACKEND / "app.py").read_text()
    start = app.index("def _status_fields_locked")
    body = app[start:app.index("\ndef ", start + 1)]
    py_keys = set(re.findall(r'"(\w+_config)":', body))
    js_keys = set()
    for path in STATIC.glob("aether-*.js"):
        js_keys |= set(re.findall(r"s\.(\w+_config)\b", path.read_text()))
    assert py_keys == js_keys, f"status keys python {sorted(py_keys)} != js {sorted(js_keys)}"
    # scan_config must cross the seam the same way as every other config.
    assert '"scan_config": asdict(_state["scan_config"])' in body
    assert "__dict__" not in body


def test_demod_vocabulary_has_one_python_home():
    from backend.config import BOOKMARK_DEMODS, DEMODS
    assert DEMODS == ("fm", "nfm", "am")
    assert BOOKMARK_DEMODS == frozenset({*DEMODS, None})
    literal = re.compile(r"""\(["']fm["'],\s*["']nfm["'],\s*["']am["']\)|\{["']fm["'],\s*["']nfm["'],\s*["']am["']""")
    offenders = []
    for path in sorted(BACKEND.glob("*.py")):
        if path.name == "config.py":
            continue
        if literal.search(path.read_text()) or re.search(r"_ALLOWED_DEMODS\s*=\s*\{", path.read_text()):
            offenders.append(path.name)
    assert not offenders, f"demod vocabulary written outside backend/config.py: {offenders}"


def test_demod_consumers_use_the_one_home():
    from backend.config import BOOKMARK_DEMODS, DEMODS
    from backend.radio import RadioConfig, ScanRadioConfig
    from backend.store import _ALLOWED_DEMODS
    assert _ALLOWED_DEMODS == BOOKMARK_DEMODS
    for cls in (RadioConfig, ScanRadioConfig):
        try:
            cls(demod="usb")
        except ValueError:
            pass
        else:
            raise AssertionError(f"{cls.__name__} accepted demod outside DEMODS")
        assert cls().demod in DEMODS


def test_html_demod_controls_match_the_one_home():
    from backend.config import DEMODS
    html = INDEX.read_text()
    seg = re.findall(r'data-demod="(\w+)"', html)
    assert tuple(seg) == DEMODS, f"radio demod buttons {seg} != {DEMODS}"
    select = html[html.index('id="iq-play-demod"'):]
    options = re.findall(r'<option value="(\w+)"', select[:select.index("</select>")])
    assert tuple(options) == DEMODS, f"iq-play demod options {options} != {DEMODS}"


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
