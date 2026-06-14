"""Tests for IQ-playback DSP: mix + resample to the demod rate. Hardware-free."""

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from backend.iq_playback import shift_and_resample  # noqa: E402

OUT = 2_000_000  # demod input rate (radio.FS_IN)


def _tone(freq_hz, n, fs):
    t = np.arange(n)
    return np.exp(1j * 2 * np.pi * (freq_hz / fs) * t).astype(np.complex64)


def _peak_hz(iq, fs):
    n = len(iq)
    sp = np.abs(np.fft.fftshift(np.fft.fft(iq * np.hanning(n))))
    return (int(np.argmax(sp)) - n / 2) * (fs / n)


def test_shift_brings_offset_tone_to_baseband():
    fs = 8_000_000
    iq = _tone(1_000_000, 80_000, fs)          # tone at +1 MHz in the band
    out = shift_and_resample(iq, fs, 1_000_000)  # shift +1 MHz down to baseband
    assert abs(_peak_hz(out, OUT)) < 5_000       # now near 0 Hz


def test_offset_zero_is_plain_decimation():
    fs = 8_000_000
    iq = _tone(200_000, 80_000, fs)             # 200 kHz tone, no shift
    out = shift_and_resample(iq, fs, 0)
    assert abs(_peak_hz(out, OUT) - 200_000) < 5_000   # stays at 200 kHz


def test_resample_length_ratio():
    # 10 MSps -> 2 MSps is a /5 decimation.
    out = shift_and_resample(_tone(0, 100_000, 10_000_000), 10_000_000, 0)
    assert abs(len(out) - 100_000 * OUT / 10_000_000) < 20


def test_equal_rate_is_passthrough_length():
    out = shift_and_resample(_tone(0, 4096, OUT), OUT, 0)
    assert len(out) == 4096


def test_empty_input():
    out = shift_and_resample(np.array([], dtype=np.complex64), 8_000_000, 0)
    assert len(out) == 0


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
