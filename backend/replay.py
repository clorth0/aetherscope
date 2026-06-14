"""Offline IQ replay: turn a saved cs8 capture into a scrolling spectrogram.

Reads a recorded .iq file and emits one FFT "frame" per step (same shape as a
live sweep event: f0, f1, powers), so the existing FFT + waterfall renderer
plays it back. No HackRF needed.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)


def cs8_to_complex(raw: bytes) -> np.ndarray:
    a = np.frombuffer(raw, dtype=np.int8)
    n = (len(a) // 2) * 2
    a = a[:n].astype(np.float32) / 128.0
    return (a[0::2] + 1j * a[1::2]).astype(np.complex64)


def iq_frame(iq: np.ndarray, fft_size: int = 1024) -> np.ndarray:
    """Averaged periodogram of an IQ block -> dB, fftshifted (-Fs/2 .. +Fs/2)."""
    if len(iq) == 0:
        return np.array([], dtype=np.float64)
    if len(iq) < fft_size:
        fft_size = 1 << int(np.floor(np.log2(len(iq))))  # largest pow2 <= len
        if fft_size < 8:
            return np.array([], dtype=np.float64)
    n_chunks = len(iq) // fft_size
    win = np.hanning(fft_size).astype(np.float32)
    acc = np.zeros(fft_size)
    for k in range(n_chunks):
        seg = iq[k * fft_size:(k + 1) * fft_size] * win
        acc += np.abs(np.fft.fftshift(np.fft.fft(seg))) ** 2
    acc /= n_chunks
    return (10.0 * np.log10(acc / fft_size + 1e-12)).astype(np.float64)


FrameCallback = Callable[[float, float, list], None]  # (f0_hz, f1_hz, powers)
DoneCallback = Callable[[str], None]                   # "completed" | "stopped"


class IqReplay:
    def __init__(self, path, center_hz, sample_rate, on_frame,
                 on_done=None, fps=20, fft_size=1024):
        self.path = path
        self.center_hz = center_hz
        self.sample_rate = sample_rate
        self.on_frame = on_frame
        self.on_done = on_done
        self.fps = fps
        self.fft_size = fft_size
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        f0 = self.center_hz - self.sample_rate / 2.0
        f1 = self.center_hz + self.sample_rate / 2.0
        block_samples = max(self.fft_size, int(self.sample_rate // self.fps))
        block_bytes = block_samples * 2
        try:
            with open(self.path, "rb") as fh:
                while not self._stop.is_set():
                    raw = fh.read(block_bytes)
                    if len(raw) < self.fft_size * 2:
                        break
                    powers = iq_frame(cs8_to_complex(raw), self.fft_size)
                    if len(powers):
                        try:
                            self.on_frame(f0, f1, powers.tolist())
                        except Exception:
                            log.exception("replay on_frame failed")
                    if self._stop.wait(1.0 / self.fps):
                        break
        except OSError:
            log.exception("replay read failed: %s", self.path)
        if self.on_done:
            self.on_done("stopped" if self._stop.is_set() else "completed")
