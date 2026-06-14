"""Decode a saved IQ capture with rtl_433.

rtl_433 reads cu8/cs16/cf32 files (not our cs8) and decodes best around
~250 kHz-1 MHz, so a capture is resampled to DECODE_RATE and converted to cu8 in
a temp file (chunked to bound memory), then `rtl_433 -r` is run over it. Decoded
events go to the same callback as live Decode mode.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading

import numpy as np

from .iq_playback import shift_and_resample
from .replay import cs8_to_complex

log = logging.getLogger(__name__)

RTL_433 = shutil.which("rtl_433") or "/opt/homebrew/bin/rtl_433"
DECODE_RATE = 1_000_000          # rtl_433-friendly rate
_CHUNK_SAMPLES = 4_000_000       # complex samples per processing chunk


def to_cu8(iq) -> bytes:
    """Convert complex baseband (-1..1) to interleaved unsigned 8-bit (cu8)."""
    iq = np.asarray(iq)
    inter = np.empty(iq.size * 2, dtype=np.float32)
    inter[0::2] = iq.real
    inter[1::2] = iq.imag
    return np.clip(np.round(inter * 127.5 + 127.5), 0, 255).astype(np.uint8).tobytes()


def _convert(path, capture_rate, out_path, stop=None) -> None:
    """Resample a cs8 .iq to DECODE_RATE and write a cu8 file, chunk by chunk."""
    block = _CHUNK_SAMPLES * 2  # cs8 bytes
    with open(path, "rb") as src, open(out_path, "wb") as dst:
        while True:
            if stop is not None and stop.is_set():
                return
            raw = src.read(block)
            if not raw:
                break
            iq = cs8_to_complex(raw)
            res = shift_and_resample(iq, capture_rate, 0, out_rate=DECODE_RATE)
            dst.write(to_cu8(res))


class IqFileDecoder:
    """Run rtl_433 over a saved .iq (resampled/converted), one-shot."""

    def __init__(self, path, capture_rate, center_hz, on_event, on_done=None):
        self.path = path
        self.capture_rate = int(capture_rate)
        self.center_hz = int(center_hz)
        self.on_event = on_event
        self.on_done = on_done
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tmp: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        p = self._proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        reason = "completed"
        try:
            fd, self._tmp = tempfile.mkstemp(suffix=".cu8")
            os.close(fd)
            _convert(self.path, self.capture_rate, self._tmp, self._stop)
            if self._stop.is_set():
                reason = "stopped"
            else:
                cmd = [RTL_433, "-r", f"cu8:{self._tmp}", "-s", str(DECODE_RATE),
                       "-f", str(self.center_hz), "-F", "json"]
                log.info("decode file: %s", " ".join(cmd))
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                assert self._proc.stdout is not None
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    try:
                        self.on_event(ev)
                    except Exception:
                        log.exception("decode on_event failed")
                self._proc.wait()
                if self._stop.is_set():
                    reason = "stopped"
        except FileNotFoundError:
            log.error("rtl_433 not found at %s", RTL_433)
            reason = "died"
        except Exception:
            log.exception("file decode failed")
            reason = "died"
        finally:
            if self._tmp and os.path.exists(self._tmp):
                try:
                    os.unlink(self._tmp)
                except OSError:
                    pass
        if self.on_done:
            try:
                self.on_done(reason)
            except Exception:
                log.exception("on_done failed")
