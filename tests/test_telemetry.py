"""Tests for runtime telemetry (backend.telemetry)."""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import telemetry  # noqa: E402


def test_is_warning_matches_problems_not_normal_lines():
    assert telemetry.is_warning("hackrf_transfer buffer overrun")
    assert telemetry.is_warning("USB error -7")
    assert telemetry.is_warning("couldn't transfer")
    # normal status lines are not warnings
    assert not telemetry.is_warning("call hackrf_sample_rate_set(20.000 MHz)")
    assert not telemetry.is_warning("5.2 MiB / 1.000 sec (5.2 MiB/second)")


def test_note_warning_counts_and_keeps_recent():
    telemetry.reset()
    telemetry.note_warning("sweep", "buffer overrun detected")
    snap = telemetry.snapshot()
    assert snap["counters"]["usb_warnings"] == 1
    assert any("overrun" in r for r in snap["recent"])


def test_watch_stderr_only_records_warnings():
    telemetry.reset()
    stream = io.BytesIO(b"5.0 MiB / 1.000 sec\nbuffer overrun\nall good\nUSB error -4\n")
    telemetry.watch_stderr("xfer", stream).join(2)
    assert telemetry.snapshot()["counters"].get("usb_warnings", 0) == 2


def test_bump_and_reset():
    telemetry.reset()
    telemetry.bump("subprocess_deaths")
    telemetry.bump("subprocess_deaths")
    assert telemetry.snapshot()["counters"]["subprocess_deaths"] == 2
    telemetry.reset()
    assert telemetry.snapshot()["counters"] == {}


if __name__ == "__main__":
    import traceback

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
