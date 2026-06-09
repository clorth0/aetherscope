"""IQ capture via `hackrf_transfer`.

Spawns hackrf_transfer to record signed 8-bit interleaved I/Q to a file
in ~/hackrf-web/captures/, with a JSON sidecar describing the capture.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

HACKRF_TRANSFER = shutil.which("hackrf_transfer") or "/opt/homebrew/bin/hackrf_transfer"

# Captures land next to the repo by default; override with HACKRF_WEB_CAPTURES_DIR
_REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURES_DIR = Path(os.environ.get("HACKRF_WEB_CAPTURES_DIR") or (_REPO_ROOT / "captures"))


def _slug(label: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", label.strip())
    return s.strip("_") or "capture"


@dataclass
class CaptureConfig:
    freq_hz: int = 433_920_000
    sample_rate: int = 8_000_000
    duration_s: float = 5.0
    lna_gain: int = 16
    vga_gain: int = 20
    amp_enable: bool = False
    label: str = ""


@dataclass
class CaptureRecord:
    """Metadata for one capture (also written as sidecar JSON)."""
    name: str
    path: str
    sidecar: str
    freq_hz: int
    sample_rate: int
    duration_s: float
    started_at: float
    finished_at: float | None
    file_size: int
    sample_format: str
    label: str


ProgressCallback = Callable[[int, int], None]   # (bytes_written, expected_bytes)
DoneCallback     = Callable[[CaptureRecord, str], None]  # (record, reason)
# reason: "completed" | "cancelled" | "died"


class IqCapture:
    SAMPLE_BYTES = 2  # cs8: 1 byte I + 1 byte Q

    def __init__(
        self,
        config: CaptureConfig,
        on_progress: ProgressCallback | None = None,
        on_done: DoneCallback | None = None,
    ):
        self.config = config
        self.on_progress = on_progress
        self.on_done = on_done
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._path: Path | None = None
        self._sidecar: Path | None = None
        self._started_at = 0.0
        self._expected_bytes = 0

    def start(self) -> CaptureRecord:
        CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        label_slug = _slug(self.config.label) if self.config.label else f"{self.config.freq_hz//1_000_000}MHz"
        base = f"{ts}_{label_slug}"
        self._path = CAPTURES_DIR / f"{base}.iq"
        self._sidecar = CAPTURES_DIR / f"{base}.json"

        self._started_at = time.time()
        self._expected_bytes = int(self.config.duration_s * self.config.sample_rate * self.SAMPLE_BYTES)

        record = self._make_record(finished_at=None, file_size=0)
        self._sidecar.write_text(json.dumps(asdict(record), indent=2))

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return record

    def cancel(self) -> None:
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

    def _make_record(self, finished_at: float | None, file_size: int) -> CaptureRecord:
        return CaptureRecord(
            name=self._path.name if self._path else "",
            path=str(self._path) if self._path else "",
            sidecar=str(self._sidecar) if self._sidecar else "",
            freq_hz=self.config.freq_hz,
            sample_rate=self.config.sample_rate,
            duration_s=self.config.duration_s,
            started_at=self._started_at,
            finished_at=finished_at,
            file_size=file_size,
            sample_format="cs8",
            label=self.config.label,
        )

    def _run(self) -> None:
        c = self.config
        n_samples = int(c.duration_s * c.sample_rate)
        cmd = [
            HACKRF_TRANSFER,
            "-r", str(self._path),
            "-f", str(c.freq_hz),
            "-s", str(c.sample_rate),
            "-n", str(n_samples),
            "-l", str(c.lna_gain),
            "-g", str(c.vga_gain),
        ]
        if c.amp_enable:
            cmd += ["-a", "1"]
        log.info("starting: %s", " ".join(cmd))

        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log.error("hackrf_transfer not found at %s", HACKRF_TRANSFER)
            return

        # Poll file size while transfer runs
        assert self._path is not None
        while True:
            if self._stop.is_set():
                break
            try:
                size = self._path.stat().st_size if self._path.exists() else 0
            except OSError:
                size = 0
            if self.on_progress:
                try:
                    self.on_progress(size, self._expected_bytes)
                except Exception:
                    log.exception("on_progress failed")
            if self._proc.poll() is not None:
                break
            time.sleep(0.2)

        final_size = self._path.stat().st_size if self._path.exists() else 0
        finished_at = time.time()
        reason = "cancelled" if self._stop.is_set() else (
            "completed" if final_size >= self._expected_bytes * 0.95 else "died"
        )

        record = self._make_record(finished_at=finished_at, file_size=final_size)
        try:
            self._sidecar.write_text(json.dumps(asdict(record), indent=2))
        except Exception:
            log.exception("failed to write sidecar")

        if self.on_done:
            try:
                self.on_done(record, reason)
            except Exception:
                log.exception("on_done failed")


def list_captures() -> list[dict]:
    """Return all captures in CAPTURES_DIR, newest first."""
    if not CAPTURES_DIR.exists():
        return []
    items: list[dict] = []
    for sidecar in CAPTURES_DIR.glob("*.json"):
        try:
            data = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        # refresh size from disk in case the run is still in flight
        try:
            data["file_size"] = os.path.getsize(data["path"])
        except OSError:
            pass
        items.append(data)
    items.sort(key=lambda d: d.get("started_at", 0), reverse=True)
    return items


def delete_capture(name: str) -> bool:
    """Delete a capture .iq + sidecar by base filename."""
    if "/" in name or ".." in name:
        return False
    base = name.removesuffix(".iq").removesuffix(".json")
    iq = CAPTURES_DIR / f"{base}.iq"
    sc = CAPTURES_DIR / f"{base}.json"
    removed = False
    for p in (iq, sc):
        try:
            if p.exists():
                p.unlink()
                removed = True
        except OSError:
            log.exception("failed to delete %s", p)
    return removed
