"""HackRF sweep streamer.

Spawns `hackrf_sweep` as a subprocess, parses its CSV output, and emits
one assembled FFT row per full sweep cycle.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

HACKRF_SWEEP = shutil.which("hackrf_sweep") or "/opt/homebrew/bin/hackrf_sweep"


@dataclass
class SweepConfig:
    f_start_mhz: int = 88
    f_stop_mhz: int = 1000
    bin_width_hz: int = 500_000
    lna_gain: int = 16       # 0–40 in steps of 8
    vga_gain: int = 20       # 0–62 in steps of 2
    amp_enable: bool = False  # 14 dB RF amp


SweepCallback = Callable[[np.ndarray, np.ndarray], None]


class SweepStreamer:
    def __init__(self, config: SweepConfig, on_sweep: SweepCallback):
        self.config = config
        self.on_sweep = on_sweep
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
        cmd = [
            HACKRF_SWEEP,
            "-f", f"{c.f_start_mhz}:{c.f_stop_mhz}",
            "-w", str(c.bin_width_hz),
            "-l", str(c.lna_gain),
            "-g", str(c.vga_gain),
        ]
        if c.amp_enable:
            cmd += ["-a", "1"]

        log.info("starting: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            log.error("hackrf_sweep binary not found at %s", HACKRF_SWEEP)
            return

        accumulators: dict[int, float] = {}
        last_hz_low = -1

        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            parts = line.strip().split(", ")
            if len(parts) < 7:
                continue
            try:
                hz_low = int(parts[2])
                bin_width = float(parts[4])
                powers = [float(x) for x in parts[6:]]
            except (ValueError, IndexError):
                continue

            # New sweep when hz_low resets to the start of the range
            if hz_low < last_hz_low and accumulators:
                self._emit(accumulators)
                accumulators = {}
            last_hz_low = hz_low

            for i, p in enumerate(powers):
                f = int(hz_low + i * bin_width)
                accumulators[f] = p

        log.info("sweep streamer exiting")

    def _emit(self, accumulators: dict[int, float]) -> None:
        freqs = np.fromiter(sorted(accumulators.keys()), dtype=np.float64)
        powers = np.fromiter((accumulators[int(f)] for f in freqs), dtype=np.float32, count=len(freqs))
        try:
            self.on_sweep(freqs, powers)
        except Exception:
            log.exception("on_sweep callback failed")
