"""ADS-B receiver via readsb-hackrf.

Spawns readsb with `--device-type=hackrf` writing aircraft.json into a
tempdir. A poller thread reads aircraft.json every second and pushes the
list to a callback.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

from .worker import WorkerLifecycle

READSB = (
    shutil.which("readsb-hackrf")
    or str(Path.home() / ".local" / "bin" / "readsb-hackrf")
)
POLL_INTERVAL = 1.0


def _optional_coord(value: float | None, low: float, high: float, label: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f"{label} must be {low:g} to {high:g}")


@dataclass
class AdsbConfig:
    gain_db: int = 32           # readsb gain in dB (0-62 for HackRF)
    rx_lat: float | None = None  # optional receiver location for distance calc
    rx_lon: float | None = None

    def __post_init__(self) -> None:
        gain = self.gain_db
        if (
            isinstance(gain, bool)
            or not isinstance(gain, (int, float))
            or not math.isfinite(gain)
            or not 0 <= gain <= 62
        ):
            raise ValueError("ADS-B gain must be 0 to 62 dB")
        _optional_coord(self.rx_lat, -90, 90, "Latitude")
        _optional_coord(self.rx_lon, -180, 180, "Longitude")


AircraftCallback = Callable[[list[dict], dict], None]   # (aircraft, meta)
ExitCallback     = Callable[[str], None]


class AdsbReceiver(WorkerLifecycle):
    def __init__(
        self,
        config: AdsbConfig,
        on_update: AircraftCallback,
        on_exit: ExitCallback | None = None,
    ):
        self.config = config
        self.on_update = on_update
        self.on_exit = on_exit
        # ADS-B waited 3 s for readsb (the rest wait 2 s): intentional.
        super().__init__(wait_timeout=3)
        self._tmp: Path | None = None

    def _cleanup_on_stop(self) -> None:
        if self._tmp:
            shutil_rmtree_safe(self._tmp)
            self._tmp = None

    def _run(self) -> None:
        c = self.config
        self._tmp = Path(tempfile.mkdtemp(prefix="readsb-hackrf-"))
        cmd = [
            READSB,
            "--device-type", "hackrf",
            "--gain", str(c.gain_db),
            "--quiet",
            "--write-json", str(self._tmp),
            "--write-json-every", "1",
            "--json-location-accuracy", "2",
        ]
        if c.rx_lat is not None and c.rx_lon is not None:
            cmd += ["--lat", str(c.rx_lat), "--lon", str(c.rx_lon)]

        log.info("starting: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log.error("readsb-hackrf not found at %s", READSB)
            return

        aircraft_json = self._tmp / "aircraft.json"
        while not self._stop.is_set():
            if self._proc.poll() is not None:
                log.warning("readsb exited unexpectedly")
                break
            try:
                if aircraft_json.exists():
                    data = json.loads(aircraft_json.read_text())
                    aircraft = data.get("aircraft", []) or []
                    meta = {
                        "now": data.get("now"),
                        "messages": data.get("messages", 0),
                        "aircraft_count": len(aircraft),
                    }
                    try:
                        self.on_update(aircraft, meta)
                    except Exception:
                        log.exception("on_update callback failed")
            except (OSError, json.JSONDecodeError):
                pass
            # Event-based wait so stop() interrupts immediately instead of
            # making the user wait up to POLL_INTERVAL seconds.
            if self._stop.wait(POLL_INTERVAL):
                break

        log.info("adsb receiver exiting")
        self.emit_exit(self.on_exit, completed=False)


def shutil_rmtree_safe(path: Path) -> None:
    import shutil as _sh
    try:
        _sh.rmtree(path, ignore_errors=True)
    except Exception:
        log.exception("failed to remove %s", path)
