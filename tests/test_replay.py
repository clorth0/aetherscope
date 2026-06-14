"""Tests for IQ replay framing (backend.replay)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from backend.replay import cs8_to_complex, iq_frame  # noqa: E402


def _cs8_tone(norm_freq, n):
    """cs8 bytes for a complex tone at `norm_freq` cycles/sample."""
    t = np.arange(n)
    iq = np.exp(1j * 2 * np.pi * norm_freq * t)
    raw = np.empty(n * 2, dtype=np.int8)
    raw[0::2] = np.clip(np.round(iq.real * 100), -127, 127)
    raw[1::2] = np.clip(np.round(iq.imag * 100), -127, 127)
    return raw.tobytes()


def test_cs8_to_complex_roundtrip_shape():
    c = cs8_to_complex(_cs8_tone(0.1, 1000))
    assert len(c) == 1000
    assert c.dtype == np.complex64


def test_iq_frame_peak_at_tone_offset():
    # Tone at +0.25 cycles/sample. After fftshift, bin 0 = -Fs/2, so a +Fs/4
    # tone lands at 0.75 * N = ~768 for N=1024.
    c = cs8_to_complex(_cs8_tone(0.25, 8192))
    powers = iq_frame(c, fft_size=1024)
    assert len(powers) == 1024
    peak = int(np.argmax(powers))
    assert abs(peak - 768) < 20, f"peak bin {peak}, expected ~768"


def test_iq_frame_handles_short_input():
    powers = iq_frame(cs8_to_complex(_cs8_tone(0.1, 300)), fft_size=1024)
    assert len(powers) > 0  # falls back to a smaller FFT, no crash


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
