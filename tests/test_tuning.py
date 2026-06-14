"""Tests for snap-to-peak frequency offset finding (backend.tuning)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from backend.tuning import find_peak_offset  # noqa: E402

FS = 2_000_000


def _tone(freq_hz, n, amp=1.0):
    """Complex baseband tone at freq_hz (offset from center) over n samples."""
    t = np.arange(n)
    return (amp * np.exp(1j * 2 * np.pi * (freq_hz / FS) * t)).astype(np.complex64)


def _noise(n, std, seed):
    rng = np.random.default_rng(seed)
    return ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * std).astype(np.complex64)


def test_locks_onto_positive_offset_tone():
    off = find_peak_offset(_tone(30_000, 65536), FS)
    assert abs(off - 30_000) < 500, off


def test_resolves_negative_offset():
    off = find_peak_offset(_tone(-40_000, 65536), FS)
    assert abs(off - (-40_000)) < 500, off


def test_ignores_dc_spike():
    # A huge DC / LO-leakage offset must not win; the weak +20 kHz tone should.
    n = 65536
    iq = _tone(20_000, n, amp=1.0) + np.complex64(50.0) + _noise(n, 0.05, seed=1)
    off = find_peak_offset(iq, FS)
    assert abs(off - 20_000) < 500, off


def test_returns_zero_on_noise():
    off = find_peak_offset(_noise(262144, 1.0, seed=2), FS)
    assert off == 0.0, off


def test_respects_search_window():
    # Only tone is at +150 kHz, outside the +-100 kHz search window -> no snap.
    n = 262144
    iq = _tone(150_000, n, amp=1.0) + _noise(n, 0.3, seed=3)
    off = find_peak_offset(iq, FS, search_hz=100_000)
    assert off == 0.0, off


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
