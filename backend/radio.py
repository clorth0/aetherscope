"""AM/FM radio receiver: hackrf_transfer I/Q -> demodulated mono audio.

Streams signed 8-bit interleaved I/Q from hackrf_transfer, demodulates a
tuned frequency to mono int16 PCM at AUDIO_RATE, and hands each block to a
callback.

FM is wideband (broadcast 88-108 MHz). AM is envelope-detected, useful for
airband (118-137 MHz) and other AM signals above the HackRF's 1 MHz tuning
floor (mediumwave broadcast is below that floor and not supported).

The fixed-rate decimation chain (2_000_000 -> 250_000 -> 50_000) keeps every
stage an integer downsample, and all filters carry their state across blocks
(lfilter zi) so there are no seams between blocks.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy.signal import firwin, lfilter

log = logging.getLogger(__name__)

HACKRF_TRANSFER = shutil.which("hackrf_transfer") or "/opt/homebrew/bin/hackrf_transfer"

FS_IN = 2_000_000          # HackRF minimum sample rate
DECIM1 = 8                 # 2_000_000 -> 250_000
IF_RATE = FS_IN // DECIM1  # 250_000
DECIM2 = 5                 # 250_000 -> 50_000
AUDIO_RATE = IF_RATE // DECIM2  # 50_000
# 0.1 s per block; a multiple of DECIM1*DECIM2 so downsample phase stays aligned.
BLOCK_SAMPLES = 200_000
SIGNAL_EVERY = 5           # emit a signal-strength reading every Nth block (~2 Hz)

# Fixed filters (rates are fixed).
_B_STAGE1 = firwin(63, 100_000, fs=FS_IN)        # anti-alias for the /8 stage
_B_FM_AUDIO = firwin(63, 15_000, fs=IF_RATE)     # FM mono audio lowpass before /5
_B_AM_CHAN = firwin(63, 8_000, fs=IF_RATE)       # AM channel lowpass before /5

# 75 us de-emphasis (US FM) at AUDIO_RATE, single-pole IIR.
_DEEMPH_POLE = float(np.exp(-1.0 / (AUDIO_RATE * 75e-6)))
_B_DEEMPH = [1.0 - _DEEMPH_POLE]
_A_DEEMPH = [1.0, -_DEEMPH_POLE]

# DC blocker for AM envelope at AUDIO_RATE (cutoff ~8 Hz).
_B_DCB = [1.0, -1.0]
_A_DCB = [1.0, -0.995]

_FM_GAIN = 12_000.0   # discriminator output (radians) -> int16
_AM_GAIN = 8_000.0    # envelope -> int16


def make_state(demod: str) -> dict:
    """Fresh per-receiver filter state. One state object per start()."""
    state: dict = {"zi1": np.zeros(len(_B_STAGE1) - 1, dtype=np.complex128)}
    if demod == "fm":
        state["last"] = np.complex128(0)
        state["zi_audio"] = np.zeros(len(_B_FM_AUDIO) - 1)
        state["zi_deemph"] = np.zeros(1)
    else:  # am
        state["zi_chan"] = np.zeros(len(_B_AM_CHAN) - 1, dtype=np.complex128)
        state["zi_dcb"] = np.zeros(1)
    return state


def demod_fm(iq: np.ndarray, state: dict) -> np.ndarray:
    x, state["zi1"] = lfilter(_B_STAGE1, [1.0], iq, zi=state["zi1"])
    x = x[::DECIM1]

    # FM discriminator with cross-block continuity.
    prev = np.empty(len(x), dtype=np.complex128)
    prev[0] = state["last"]
    prev[1:] = x[:-1]
    disc = np.angle(x * np.conj(prev))
    if len(x):
        state["last"] = x[-1]

    a, state["zi_audio"] = lfilter(_B_FM_AUDIO, [1.0], disc, zi=state["zi_audio"])
    a = a[::DECIM2]
    a, state["zi_deemph"] = lfilter(_B_DEEMPH, _A_DEEMPH, a, zi=state["zi_deemph"])
    return _to_int16(a * _FM_GAIN)


def demod_am(iq: np.ndarray, state: dict) -> np.ndarray:
    x, state["zi1"] = lfilter(_B_STAGE1, [1.0], iq, zi=state["zi1"])
    x = x[::DECIM1]
    x, state["zi_chan"] = lfilter(_B_AM_CHAN, [1.0], x, zi=state["zi_chan"])
    x = x[::DECIM2]

    env = np.abs(x)  # envelope detection (offset-invariant)
    a, state["zi_dcb"] = lfilter(_B_DCB, _A_DCB, env, zi=state["zi_dcb"])
    return _to_int16(a * _AM_GAIN)


def demodulate(iq: np.ndarray, demod: str, state: dict) -> np.ndarray:
    """Demodulate a complex baseband block to mono int16 PCM at AUDIO_RATE."""
    if demod == "fm":
        return demod_fm(iq, state)
    return demod_am(iq, state)


def _to_int16(samples: np.ndarray) -> np.ndarray:
    return np.clip(samples, -32768, 32767).astype(np.int16)


def signal_dbfs(iq: np.ndarray) -> float:
    """Channel power (within ~+-100 kHz of the tuned frequency) in dBFS.

    Uses the same anti-alias filter as the demod chain so adjacent channels
    do not inflate the reading, then reports mean power relative to full
    scale. The HackRF has a strong DC / LO-leakage spike at the tuned center,
    so the constant offset (the block mean) is removed first; otherwise that
    artifact dominates and every frequency looks alive. The HackRF has no
    absolute calibration, so this is a relative figure useful for "is there
    signal here".
    """
    iq = iq - iq.mean()  # drop the DC / LO-leakage spike at center
    x = lfilter(_B_STAGE1, [1.0], iq)
    p = float(np.mean(x.real ** 2 + x.imag ** 2))
    return 10.0 * np.log10(p + 1e-12)


@dataclass
class RadioConfig:
    demod: str = "fm"             # "fm" (wideband) | "am"
    freq_mhz: float = 101.1
    lna_gain: int = 16            # 0-40 in steps of 8
    vga_gain: int = 20            # 0-62 in steps of 2
    amp_enable: bool = False      # 14 dB RF amp
    sample_rate_hz: int = FS_IN


AudioCallback = Callable[[bytes], None]
SignalCallback = Callable[[float], None]  # channel power in dBFS
ExitCallback = Callable[[str], None]  # reason: "stopped" | "died"


class RadioReceiver:
    def __init__(
        self,
        config: RadioConfig,
        on_audio: AudioCallback,
        on_exit: ExitCallback | None = None,
        on_signal: SignalCallback | None = None,
    ):
        self.config = config
        self.on_audio = on_audio
        self.on_exit = on_exit
        self.on_signal = on_signal
        self._proc: subprocess.Popen | None = None
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
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        c = self.config
        freq_hz = int(c.freq_mhz * 1_000_000)
        cmd = [
            HACKRF_TRANSFER,
            "-r", "-",                       # stream I/Q to stdout
            "-f", str(freq_hz),
            "-s", str(c.sample_rate_hz),
            "-b", "1750000",                 # baseband filter bandwidth
            "-l", str(c.lna_gain),
            "-g", str(c.vga_gain),
        ]
        if c.amp_enable:
            cmd += ["-a", "1"]

        log.info("starting: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            log.error("hackrf_transfer not found at %s", HACKRF_TRANSFER)
            if self.on_exit:
                self.on_exit("died")
            return

        state = make_state(c.demod)
        block_bytes = BLOCK_SAMPLES * 2  # cs8: 2 signed bytes per complex sample
        block_i = 0

        assert self._proc.stdout is not None
        while not self._stop.is_set():
            buf = self._proc.stdout.read(block_bytes)
            if not buf:
                break
            raw = np.frombuffer(buf, dtype=np.int8)
            n = (len(raw) // 2) * 2
            if n < 2:
                continue
            raw = raw[:n].astype(np.float32) / 128.0
            iq = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
            try:
                pcm = demodulate(iq, c.demod, state)
                self.on_audio(pcm.tobytes())
                if self.on_signal and block_i % SIGNAL_EVERY == 0:
                    self.on_signal(signal_dbfs(iq))
            except Exception:
                log.exception("demodulation failed")
            block_i += 1

        log.info("radio receiver exiting")
        if self.on_exit:
            reason = "stopped" if self._stop.is_set() else "died"
            try:
                self.on_exit(reason)
            except Exception:
                log.exception("on_exit callback failed")
