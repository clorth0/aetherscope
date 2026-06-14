"""gpsd client for optional capture geotagging.

Talks to a local gpsd over a raw TCP socket (no extra dependency): connect,
send `?WATCH={"enable":true,"json":true}`, read line-delimited JSON, and keep a
thread-safe "last known position" from TPV (position/fix) and SKY (DOP/sats)
reports.

Privacy: geotagging is opt-in and default-off (the client only connects while
enabled). This module NEVER logs latitude/longitude; it logs fix status only
(mode, satellite count). Position is used for location only; capture timestamps
come from the system clock (the BU-353S4 has no PPS, so GPS time jitters).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

GPSD_HOST = os.environ.get("AETHERSCOPE_GPSD_HOST", "127.0.0.1")
_gpsd_port_env = os.environ.get("AETHERSCOPE_GPSD_PORT", "2947")
GPSD_PORT = int(_gpsd_port_env) if _gpsd_port_env.isdigit() else 2947

STALE_AFTER_S = 5.0          # no TPV within this many seconds -> position is stale
_MAX_LINE = 65536            # ignore absurdly long lines (malformed/hostile)
_MAX_BUF = 262144            # cap the read buffer
_WATCH = b'?WATCH={"enable":true,"json":true}\n'


# ---------------------------------------------------------------------------
# Pure parsing helpers (unit-tested)
# ---------------------------------------------------------------------------

def tpv_fields(obj: dict) -> dict:
    """Extract position fields from a gpsd TPV report.

    Returns mode always; lat/lon/alt/gps_time only when mode >= 2 (a real fix),
    else None. Altitude prefers altMSL (mean sea level), falling back to alt.
    """
    try:
        mode = int(obj.get("mode", 0) or 0)
    except (TypeError, ValueError):
        mode = 0
    out: dict = {"mode": mode, "lat": None, "lon": None, "alt": None,
                 "gps_time": None, "eph": None}
    if mode >= 2:
        out["lat"] = obj.get("lat")
        out["lon"] = obj.get("lon")
        alt = obj.get("altMSL")
        out["alt"] = alt if alt is not None else obj.get("alt")
        out["gps_time"] = obj.get("time")
        out["eph"] = obj.get("eph")
    return out


def sky_fields(obj: dict) -> dict:
    """Extract DOP / satellite count from a gpsd SKY report."""
    out: dict = {}
    if "uSat" in obj:
        out["sats"] = obj.get("uSat")
    if "hdop" in obj:
        out["hdop"] = obj.get("hdop")
    return out


# Stored-geotag precision: a privacy control over what lands in capture files.
# The live (local) display always shows full precision.
_PRECISION_DECIMALS = {"full": None, "100m": 3, "1km": 2}


def coarsen_geolocation(geo: dict | None, precision: str) -> dict | None:
    """Round a geolocation's lat/lon for privacy before it is stored.

    "full" leaves coordinates exact; "100m" rounds to ~0.001 deg; "1km" to
    ~0.01 deg. Tags the result with the precision used. None passes through.
    """
    if geo is None:
        return None
    out = dict(geo)
    dec = _PRECISION_DECIMALS.get(precision)
    if dec is None:
        out["precision"] = "full"
        return out
    if out.get("lat") is not None:
        out["lat"] = round(out["lat"], dec)
    if out.get("lon") is not None:
        out["lon"] = round(out["lon"], dec)
    out["precision"] = "~100m" if dec == 3 else "~1km"
    return out


def split_json_lines(buf: bytes) -> tuple[list, bytes]:
    """Split a byte buffer into parsed JSON objects + the unconsumed remainder.

    Newline-delimited. Malformed or over-long lines are skipped. The trailing
    partial line (no terminating newline) is returned as the remainder.
    """
    objs: list = []
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        line = line.strip()
        if not line or len(line) > _MAX_LINE:
            continue
        try:
            objs.append(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            continue
    return objs, buf


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

StatusCallback = Callable[[dict], None]


class GpsClient:
    """Background gpsd reader maintaining a thread-safe last-known position."""

    def __init__(
        self,
        host: str = GPSD_HOST,
        port: int = GPSD_PORT,
        enabled: bool = False,
        stale_after_s: float = STALE_AFTER_S,
        on_status: StatusCallback | None = None,
    ):
        self.host = host
        self.port = port
        self.stale_after_s = stale_after_s
        self.on_status = on_status
        self._enabled = bool(enabled)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._connected = False
        self._updated_at: float | None = None
        # Position state, merged from TPV/SKY.
        self._state = {"mode": 0, "lat": None, "lon": None, "alt": None,
                       "gps_time": None, "eph": None, "sats": None, "hdop": None}

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_sock()

    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        on = bool(on)
        with self._lock:
            if on == self._enabled:
                return
            self._enabled = on
            if not on:
                # Drop position immediately so nothing stale lingers.
                self._reset_state_locked()
                self._close_sock()
        log.info("GPS geotagging %s", "enabled" if on else "disabled")
        self._emit()

    # -- state merge (unit-tested) --------------------------------------

    def apply(self, obj: dict) -> None:
        """Merge one gpsd report (TPV or SKY) into the position state."""
        cls = obj.get("class")
        with self._lock:
            if cls == "TPV":
                self._state.update(tpv_fields(obj))
                self._updated_at = time.time()
            elif cls == "SKY":
                self._state.update(sky_fields(obj))
            else:
                return
        self._emit()

    def position(self, now: float | None = None) -> dict:
        """Thread-safe snapshot of the current position + fix quality.

        When disabled, returns a position with no coordinates. lat/lon are only
        ever populated for a real fix (mode >= 2).
        """
        if now is None:
            now = time.time()
        with self._lock:
            if not self._enabled:
                return {"enabled": False, "connected": False, "mode": 0,
                        "lat": None, "lon": None, "alt": None, "hdop": None,
                        "sats": None, "gps_time": None, "stale": True,
                        "updated_at": None}
            s = self._state
            has_fix = s["mode"] >= 2 and s["lat"] is not None
            stale = self._updated_at is None or (now - self._updated_at) > self.stale_after_s
            return {
                "enabled": True,
                "connected": self._connected,
                "mode": s["mode"],
                "lat": s["lat"] if has_fix else None,
                "lon": s["lon"] if has_fix else None,
                "alt": s["alt"] if has_fix else None,
                "hdop": s["hdop"],
                "sats": s["sats"],
                "gps_time": s["gps_time"],
                "stale": stale,
                "updated_at": self._updated_at,
            }

    def geolocation(self) -> dict | None:
        """Capture-time geotag dict if a usable fix exists, else None.

        Full precision by design; redaction happens at the capture layer. Shape
        maps cleanly onto SigMF core:geolocation later.
        """
        p = self.position()
        if not p["enabled"] or p["lat"] is None or p["stale"]:
            return None
        return {"lat": p["lat"], "lon": p["lon"], "alt_m": p["alt"],
                "mode": p["mode"], "hdop": p["hdop"], "sats": p["sats"],
                "gps_time": p["gps_time"], "source": "gpsd"}

    # -- internals ------------------------------------------------------

    def _reset_state_locked(self) -> None:
        self._state = {"mode": 0, "lat": None, "lon": None, "alt": None,
                       "gps_time": None, "eph": None, "sats": None, "hdop": None}
        self._updated_at = None

    def _emit(self) -> None:
        if self.on_status:
            try:
                self.on_status(self.position())
            except Exception:
                log.exception("gps on_status callback failed")

    def _close_sock(self) -> None:
        sock, self._sock = self._sock, None
        self._connected = False
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self._enabled:
                self._stop.wait(0.5)
                continue
            try:
                # Use a LOCAL socket ref through the read loop. set_enabled(False)
                # may null/close self._sock from another thread; reading a local
                # ref turns that into a clean OSError (caught below) instead of an
                # AttributeError on None, which would otherwise kill this thread.
                sock = socket.create_connection((self.host, self.port), timeout=5)
                self._sock = sock
                self._connected = True
                backoff = 1.0
                sock.sendall(_WATCH)
                sock.settimeout(5)
                log.info("gpsd connected at %s:%s", self.host, self.port)
                buf = b""
                while self._enabled and not self._stop.is_set():
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        self._emit()  # refresh staleness even when quiet
                        continue
                    if not data:
                        break  # gpsd closed the connection
                    buf += data
                    if len(buf) > _MAX_BUF:
                        buf = buf[-_MAX_BUF:]
                    objs, buf = split_json_lines(buf)
                    for o in objs:
                        self.apply(o)
            except Exception as e:
                # Never let the reader thread die; just reconnect.
                log.warning("gpsd connection error (%s); retrying", e.__class__.__name__)
            finally:
                self._close_sock()
            if self._enabled and not self._stop.is_set():
                self._emit()  # let the UI see "no fix / disconnected"
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
