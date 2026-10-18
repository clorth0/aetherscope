"""HackRF sweep streamer.

Spawns `hackrf_sweep` as a subprocess, parses its CSV output, and emits
one assembled FFT row per full sweep cycle.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

import numpy as np

from . import telemetry
from .worker import WorkerLifecycle

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

    def __post_init__(self) -> None:
        if not (1 <= self.f_start_mhz < self.f_stop_mhz <= 6000):
            raise ValueError(
                f"sweep range must sit inside 1-6000 MHz, got {self.f_start_mhz}-{self.f_stop_mhz}"
            )
        if self.bin_width_hz < 2_000 or self.bin_width_hz > 5_000_000:
            raise ValueError(f"bin width out of range: {self.bin_width_hz}")
        if not (0 <= self.lna_gain <= 40):
            raise ValueError(f"lna gain out of range: {self.lna_gain}")


SweepCallback = Callable[[np.ndarray, np.ndarray], None]
ExitCallback  = Callable[[str], None]   # reason: "stopped" | "died"


def _to_arrays(acc: dict[int, float]) -> tuple[np.ndarray, np.ndarray]:
    freqs = np.fromiter(sorted(acc.keys()), dtype=np.float64)
    powers = np.fromiter(
        (acc[int(f)] for f in freqs), dtype=np.float32, count=len(freqs)
    )
    return freqs, powers


def assemble_sweeps(lines: Iterable[str]) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Parse hackrf_sweep CSV output, yielding one (freqs, powers) pair per
    complete sweep cycle.

    hackrf_sweep emits each tuning segment of a sweep once, in a NON-monotonic
    order, and repeats the identical segment set every cycle. We key power by
    absolute frequency in a dict (which dedupes and lets us sort), and flush a
    full row when the first segment we saw recurs -- that marks the start of
    the next cycle. The final, still-incomplete cycle is not flushed.

    The previous implementation flushed whenever hz_low decreased, which
    assumed ascending segment order; against the real out-of-order stream it
    fired hundreds of partial rows per second instead of one full row per
    sweep, so the UI never received a coherent spectrum.
    """
    accumulators: dict[int, float] = {}
    cycle_start_hz: int | None = None

    for line in lines:
        parts = line.strip().split(", ")
        if len(parts) < 7:
            continue
        try:
            hz_low = int(parts[2])
            bin_width = float(parts[4])
            powers = [float(x) for x in parts[6:]]
        except (ValueError, IndexError):
            continue

        if cycle_start_hz is None:
            cycle_start_hz = hz_low
        elif hz_low == cycle_start_hz and accumulators:
            yield _to_arrays(accumulators)
            accumulators = {}

        for i, p in enumerate(powers):
            f = int(hz_low + i * bin_width)
            accumulators[f] = p


class SweepStreamer(WorkerLifecycle):
    def __init__(
        self,
        config: SweepConfig,
        on_sweep: SweepCallback,
        on_exit: ExitCallback | None = None,
    ):
        self.config = config
        self.on_sweep = on_sweep
        self.on_exit = on_exit
        super().__init__()
        self._got_data = False

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
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            log.error("hackrf_sweep binary not found at %s", HACKRF_SWEEP)
            return
        if self._proc.stderr is not None:
            telemetry.watch_stderr("sweep", self._proc.stderr)

        assert self._proc.stdout is not None

        def stop_aware_lines() -> Iterator[str]:
            for line in self._proc.stdout:  # type: ignore[union-attr]
                if self._stop.is_set():
                    return
                yield line

        for freqs, powers in assemble_sweeps(stop_aware_lines()):
            if self._stop.is_set():
                break
            self._got_data = True
            try:
                self.on_sweep(freqs, powers)
            except Exception:
                log.exception("on_sweep callback failed")

        log.info("sweep streamer exiting")
        self.emit_exit(self.on_exit, completed=False)

