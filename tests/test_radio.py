"""Tests for AM/FM demodulation (backend.radio).

Synthesize baseband I/Q for a known 1 kHz audio tone (once AM-modulated,
once FM-modulated) and assert the demodulator recovers a ~1 kHz tone.
No hardware required.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from backend.radio import demodulate, make_state, signal_dbfs, AUDIO_RATE, FS_IN  # noqa: E402

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


def test_nfm_recovers_tone():
    # Narrowband FM: ~3 kHz deviation (vs 75 kHz for broadcast).
    dur, dev = 0.5, 3_000.0
    n = int(FS_IN * dur)
    t = np.arange(n) / FS_IN
    msg = np.cos(2 * np.pi * TONE_HZ * t)
    phase = 2 * np.pi * dev * np.cumsum(msg) / FS_IN
    iq = np.exp(1j * phase).astype(np.complex64)

    audio = demodulate(iq, "nfm", make_state("nfm"))

    assert len(audio) > 0
    peak = _dominant_freq(audio, AUDIO_RATE)
    assert abs(peak - TONE_HZ) < 80, f"NFM recovered {peak:.0f} Hz, expected ~{TONE_HZ:.0f}"


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


def test_signal_dbfs_strong_above_weak():
    np.random.seed(0)
    n = 200_000
    t = np.arange(n)
    # Strong in-channel carrier (~20 kHz from center, amplitude 0.5 -> ~-6 dBFS).
    strong = (0.5 * np.exp(1j * 2 * np.pi * 0.01 * t)).astype(np.complex64)
    # Weak broadband noise.
    weak = (0.001 * (np.random.randn(n) + 1j * np.random.randn(n))).astype(np.complex64)

    s = signal_dbfs(strong)
    w = signal_dbfs(weak)

    assert s > w + 20, f"strong {s:.1f} dBFS should exceed weak {w:.1f} dBFS by >20"
    assert -12 < s < 0, f"strong carrier should sit near -6 dBFS, got {s:.1f}"


def test_signal_dbfs_ignores_dc_offset():
    # The HackRF has a strong DC / LO-leakage spike at the tuned center. A pure
    # DC offset (no modulation) must NOT read as signal, or every dead frequency
    # looks alive.
    n = 200_000
    dc = ((0.5 + 0.5j) * np.ones(n)).astype(np.complex64)
    tone = (0.05 * np.exp(1j * 2 * np.pi * 0.01 * np.arange(n))).astype(np.complex64)
    assert signal_dbfs(tone) > signal_dbfs(dc) + 15, (
        f"DC offset {signal_dbfs(dc):.1f} dBFS should read far below a real "
        f"tone {signal_dbfs(tone):.1f} dBFS"
    )


def test_signal_dbfs_rejects_out_of_channel():
    # A carrier far outside the +-100 kHz channel (~600 kHz) should read much
    # lower than the same carrier in-channel, so adjacent stations don't fool it.
    n = 200_000
    t = np.arange(n)
    inch = (0.5 * np.exp(1j * 2 * np.pi * 0.01 * t)).astype(np.complex64)   # ~20 kHz
    outch = (0.5 * np.exp(1j * 2 * np.pi * 0.30 * t)).astype(np.complex64)  # ~600 kHz
    assert signal_dbfs(inch) > signal_dbfs(outch) + 15


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
