"""ADS-B receiver via readsb-hackrf.

Spawns readsb with `--device-type=hackrf` writing aircraft.json into a
tempdir. A poller thread reads aircraft.json every second and pushes the
list to a callback.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

READSB = (
    shutil.which("readsb-hackrf")
    or str(Path.home() / ".local" / "bin" / "readsb-hackrf")
)
POLL_INTERVAL = 1.0


@dataclass
class AdsbConfig:
    gain_db: int = 32           # readsb gain in dB (0-62 for HackRF)
    rx_lat: float | None = None  # optional receiver location for distance calc
    rx_lon: float | None = None


AircraftCallback = Callable[[list[dict], dict], None]   # (aircraft, meta)
ExitCallback     = Callable[[str], None]


class AdsbReceiver:
    def __init__(
        self,
        config: AdsbConfig,
        on_update: AircraftCallback,
        on_exit: ExitCallback | None = None,
    ):
        self.config = config
        self.on_update = on_update
        self.on_exit = on_exit
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tmp: Path | None = None

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
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        if self._tmp:
            shutil_rmtree_safe(self._tmp)
            self._tmp = None

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

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
        if self.on_exit:
            reason = "stopped" if self._stop.is_set() else "died"
            try:
                self.on_exit(reason)
            except Exception:
                log.exception("on_exit callback failed")


def shutil_rmtree_safe(path: Path) -> None:
    import shutil as _sh
    try:
        _sh.rmtree(path, ignore_errors=True)
    except Exception:
        log.exception("failed to remove %s", path)
