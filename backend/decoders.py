"""rtl_433 decoder wrapper.

Spawns `rtl_433` with HackRF as source (via SoapySDR), parses
JSON-per-line stdout, hands each event to a callback.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

from .worker import WorkerLifecycle

RTL_433 = shutil.which("rtl_433") or "/opt/homebrew/bin/rtl_433"


@dataclass
class DecodeConfig:
    freq_hz: int = 433_920_000
    sample_rate: int = 2_400_000
    gain_db: int = 32                # SoapySDR aggregated gain for HackRF
    label: str = "ISM 433"

    def __post_init__(self) -> None:
        if not (1_000_000 <= self.freq_hz <= 6_000_000_000):
            raise ValueError("Frequency must be 1 MHz to 6 GHz")
        if not isinstance(self.label, str) or len(self.label) > 80:
            raise ValueError("Label must be a string of at most 80 characters")


DecodeCallback = Callable[[dict], None]
ExitCallback   = Callable[[str], None]   # reason: "stopped" | "died"


class Rtl433Decoder(WorkerLifecycle):
    def __init__(
        self,
        config: DecodeConfig,
        on_event: DecodeCallback,
        on_exit: ExitCallback | None = None,
    ):
        self.config = config
        self.on_event = on_event
        self.on_exit = on_exit
        super().__init__()

    def _run(self) -> None:
        c = self.config
        cmd = [
            RTL_433,
            "-d", "driver=hackrf",
            "-f", str(c.freq_hz),
            "-s", str(c.sample_rate),
            "-g", str(c.gain_db),
            "-F", "json",
            "-M", "level",
            "-M", "time:iso",
        ]
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
            log.error("rtl_433 not found at %s", RTL_433)
            return

        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self.on_event(event)
            except Exception:
                log.exception("on_event callback failed")

        log.info("rtl_433 decoder exiting")
        self.emit_exit(self.on_exit, completed=False)
