"""Listen to a saved IQ capture.

`shift_and_resample` mixes a chosen offset within the captured band down to
baseband and resamples to the demod input rate, so the proven
`radio.demodulate` chain can turn a recorded `.iq` into audio. `IqAudioPlayer`
streams that audio to the browser AudioWorklet, paced to real time. Device-free
(it reads a file).
"""

from __future__ import annotations

import logging
import threading
import time
from math import gcd

import numpy as np
from scipy.signal import resample_poly

from .radio import AUDIO_RATE, FS_IN, demodulate, make_state
from .replay import cs8_to_complex

log = logging.getLogger(__name__)


def shift_and_resample(iq, capture_rate, offset_hz, out_rate=FS_IN, start_sample=0):
    """Shift `offset_hz` to baseband and resample capture_rate -> out_rate.

    The mix uses an absolute sample index (`start_sample`) so phase stays
    continuous across blocks. resample_poly also lowpasses, isolating the target
    signal (+/- out_rate/2) from the rest of the captured band.
    """
    iq = np.asarray(iq)
    if iq.size == 0:
        return iq.astype(np.complex64)
    if offset_hz:
        n = np.arange(len(iq), dtype=np.float64) + start_sample
        iq = iq * np.exp(-2j * np.pi * (offset_hz / capture_rate) * n).astype(np.complex64)
    g = gcd(int(out_rate), int(capture_rate))
    up = int(out_rate) // g
    down = int(capture_rate) // g
    if up == down:
        return iq.astype(np.complex64)
    return resample_poly(iq, up, down).astype(np.complex64)


class IqAudioPlayer:
    """Demodulate a saved .iq to audio and stream it, paced to real time."""

    def __init__(self, path, capture_rate, demod, offset_hz, on_audio, on_done=None):
        self.path = path
        self.capture_rate = int(capture_rate)
        self.demod = demod
        self.offset_hz = float(offset_hz)
        self.on_audio = on_audio
        self.on_done = on_done
        self.block_samples = max(1, self.capture_rate // 10)  # ~0.1 s of input
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
        state = make_state(self.demod)
        sample_i = 0
        block_bytes = self.block_samples * 2  # cs8: 2 bytes per complex sample
        t0 = time.monotonic()
        audio_s = 0.0  # seconds of audio emitted so far
        try:
            with open(self.path, "rb") as fh:
                while not self._stop.is_set():
                    raw = fh.read(block_bytes)
                    if not raw:
                        break
                    iq = cs8_to_complex(raw)
                    if len(iq) == 0:
                        break
                    shifted = shift_and_resample(
                        iq, self.capture_rate, self.offset_hz, start_sample=sample_i)
                    sample_i += len(iq)
                    try:
                        pcm = demodulate(shifted, self.demod, state)
                    except Exception:
                        log.exception("playback demod failed")
                        continue
                    self.on_audio(pcm.tobytes())
                    # Clock-based pacing: only sleep the slack between audio time
                    # and wall time, so processing cost does not add latency.
                    audio_s += len(pcm) / AUDIO_RATE
                    ahead = audio_s - (time.monotonic() - t0)
                    if ahead > 0 and self._stop.wait(ahead):
                        break
        except OSError:
            log.exception("iq playback read failed: %s", self.path)
        if self.on_done:
            try:
                self.on_done("stopped" if self._stop.is_set() else "completed")
            except Exception:
                log.exception("on_done failed")
