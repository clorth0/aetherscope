"""Snap-to-peak auto-tuner: find the strongest carrier near the tuned frequency.

The radio captures the full 2 MHz around the tuned frequency, so the station the
user wants is already in the receiver's I/Q. `find_peak_offset` measures how far
the strongest nearby carrier sits from center, so the receiver can be restarted
on the corrected frequency. Pure DSP, no hardware.
"""

from __future__ import annotations

import numpy as np

from .replay import iq_frame


def find_peak_offset(
    iq: np.ndarray,
    sample_rate: float,
    search_hz: float = 100_000.0,
    guard_hz: float = 5_000.0,
    min_snr_db: float = 6.0,
    fft_size: int = 4096,
) -> float:
    """Hz offset from center to the strongest carrier within +-search_hz.

    The DC / LO-leakage spike at center is removed (its constant offset would
    otherwise always be the peak), and a +-guard_hz dead zone around 0 Hz is
    skipped so residual leakage never wins. Returns 0.0 when no carrier stands
    at least min_snr_db above the window's median power, so a dead band leaves
    the tuning unchanged.
    """
    iq = np.asarray(iq)
    if iq.size == 0:
        return 0.0
    powers = iq_frame(iq - iq.mean(), fft_size)  # dB, fftshifted (-Fs/2 .. +Fs/2)
    n = len(powers)
    if n == 0:
        return 0.0

    freqs = (np.arange(n) - n // 2) * (sample_rate / n)
    mask = (np.abs(freqs) >= guard_hz) & (np.abs(freqs) <= search_hz)
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return 0.0

    win = powers[idx]
    k = idx[int(np.argmax(win))]
    if powers[k] - float(np.median(win)) < min_snr_db:
        return 0.0
    return float(freqs[k])
