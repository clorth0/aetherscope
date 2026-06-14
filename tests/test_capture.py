"""Tests for capture config validation (backend.capture.capture_config_error)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.capture import CaptureConfig, capture_config_error  # noqa: E402


def test_default_config_is_valid():
    assert capture_config_error(CaptureConfig()) is None


def test_excessive_duration_rejected():
    assert capture_config_error(CaptureConfig(duration_s=99_999)) is not None


def test_zero_duration_rejected():
    assert capture_config_error(CaptureConfig(duration_s=0)) is not None


def test_sample_rate_out_of_range_rejected():
    assert capture_config_error(CaptureConfig(sample_rate=50_000_000)) is not None
    assert capture_config_error(CaptureConfig(sample_rate=1_000_000)) is not None


def test_frequency_out_of_range_rejected():
    assert capture_config_error(CaptureConfig(freq_hz=10_000)) is not None
    assert capture_config_error(CaptureConfig(freq_hz=9_000_000_000)) is not None


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
