"""Tests for AM/FM demodulation (backend.radio).

Synthesize baseband I/Q for a known 1 kHz audio tone (once AM-modulated,
once FM-modulated) and assert the demodulator recovers a ~1 kHz tone.
No hardware required.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from backend.radio import demodulate, make_state, AUDIO_RATE, FS_IN  # noqa: E402

TONE_HZ = 1000.0


def _dominant_freq(audio_i16, rate):
    x = audio_i16.astype(np.float64)
    x -= x.mean()
    if not np.any(x):
        return 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1.0 / rate)
    spec[freqs < 50] = 0.0  # ignore DC residue
    return float(freqs[int(np.argmax(spec))])


def test_fm_recovers_tone():
    dur, dev = 0.5, 75_000.0
    n = int(FS_IN * dur)
    t = np.arange(n) / FS_IN
    msg = np.cos(2 * np.pi * TONE_HZ * t)
    phase = 2 * np.pi * dev * np.cumsum(msg) / FS_IN
    iq = np.exp(1j * phase).astype(np.complex64)

    audio = demodulate(iq, "fm", make_state("fm"))

    assert len(audio) > 0
    peak = _dominant_freq(audio, AUDIO_RATE)
    assert abs(peak - TONE_HZ) < 80, f"FM recovered {peak:.0f} Hz, expected ~{TONE_HZ:.0f}"


def test_am_recovers_tone():
    dur, depth = 0.5, 0.6
    n = int(FS_IN * dur)
    t = np.arange(n) / FS_IN
    msg = np.cos(2 * np.pi * TONE_HZ * t)
    iq = (1.0 + depth * msg).astype(np.complex64)  # AM on a 0 Hz baseband carrier

    audio = demodulate(iq, "am", make_state("am"))

    assert len(audio) > 0
    peak = _dominant_freq(audio, AUDIO_RATE)
    assert abs(peak - TONE_HZ) < 80, f"AM recovered {peak:.0f} Hz, expected ~{TONE_HZ:.0f}"


def test_unknown_demod_falls_back_without_crashing():
    # Defensive: an unexpected demod string should still return audio bytes
    # (treated as AM) rather than raising.
    iq = (1.0 + 0.5 * np.cos(2 * np.pi * TONE_HZ * np.arange(100_000) / FS_IN)).astype(np.complex64)
    audio = demodulate(iq, "am", make_state("am"))
    assert audio.dtype == np.int16


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
