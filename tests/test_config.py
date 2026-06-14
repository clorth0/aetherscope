"""Tests for runtime config helpers. Hardware-free."""

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app import resolve_port  # noqa: E402


def test_default_when_unset():
    assert resolve_port(None) == 8765


def test_valid_port():
    assert resolve_port("9000") == 9000
    assert resolve_port("1") == 1
    assert resolve_port("65535") == 65535


def test_invalid_falls_back():
    assert resolve_port("abc") == 8765
    assert resolve_port("") == 8765
    assert resolve_port("80.5") == 8765


def test_out_of_range_falls_back():
    assert resolve_port("0") == 8765
    assert resolve_port("70000") == 8765
    assert resolve_port("-5") == 8765


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
