"""Capture config bounds live on the constructor, and names share one stem helper."""

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.capture import CaptureConfig, capture_basename, delete_capture, redact_location  # noqa: E402


def _rejects(fn):
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("constructor accepted an illegal config")


def test_default_config_is_valid():
    cfg = CaptureConfig()
    assert cfg.duration_s == 5.0
    assert cfg.label == ""


def test_excessive_duration_rejected():
    _rejects(lambda: CaptureConfig(duration_s=99_999))


def test_zero_duration_rejected():
    _rejects(lambda: CaptureConfig(duration_s=0))


def test_sample_rate_out_of_range_rejected():
    _rejects(lambda: CaptureConfig(sample_rate=50_000_000))
    _rejects(lambda: CaptureConfig(sample_rate=1_000_000))


def test_frequency_out_of_range_rejected():
    _rejects(lambda: CaptureConfig(freq_hz=10_000))
    _rejects(lambda: CaptureConfig(freq_hz=9_000_000_000))


def test_long_label_rejected():
    _rejects(lambda: CaptureConfig(label="x" * 81))
    assert CaptureConfig(label="x" * 80).label == "x" * 80


def test_capture_basename_returns_the_stem():
    assert capture_basename("2026-01-01_note.iq") == "2026-01-01_note"
    assert capture_basename("clip.wav") == "clip"
    assert capture_basename("clip.json") == "clip"
    assert capture_basename("clip.sigmf-meta") == "clip"
    assert capture_basename("already-a-stem") == "already-a-stem"


def test_capture_basename_rejects_traversal():
    assert capture_basename("../etc/passwd") is None
    assert capture_basename("nested/name.iq") is None
    assert capture_basename("foo..bar.iq") is None
    assert capture_basename("") is None
    assert capture_basename(".iq") is None
    assert capture_basename(None) is None  # type: ignore[arg-type]


def test_delete_and_redact_use_the_same_rejection():
    assert delete_capture("../secret.iq") is False
    assert delete_capture("nested/name.iq") is False
    assert redact_location("../secret.iq") is False
    assert redact_location("nested/name.iq") is False


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
