"""Flask + Socket.IO server.

Single source of truth for HackRF state. All sweep/decode/capture/adsb/
scan jobs are mutually exclusive: only one can hold the HackRF at a
time. State mutations are serialized by a reentrant lock so concurrent
socket handlers can't interleave and leak subprocesses.

Background poller emits device_status every 2.5 s so the UI knows
whether a HackRF is plugged in.
"""

from __future__ import annotations

import atexit
import logging
import os
import secrets
import signal
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
from flask import Flask, jsonify, render_template, send_from_directory
from flask_socketio import SocketIO, emit

from .adsb import AdsbConfig, AdsbReceiver
from .capture import CaptureConfig, IqCapture, CAPTURES_DIR, capture_config_error, delete_capture, list_captures
from .decoders import DecodeConfig, Rtl433Decoder
from .device import probe_hackrf
from .radio import AUDIO_RATE, RadioConfig, RadioReceiver, RadioScanner, ScanRadioConfig
from .replay import IqReplay
from .scan import AutoScanner, ScanConfig
from .sdr import SweepConfig, SweepStreamer
from . import telemetry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("aetherscope")

ROOT = Path(__file__).resolve().parent.parent
app = Flask(
    __name__,
    template_folder=str(ROOT / "frontend" / "templates"),
    static_folder=str(ROOT / "frontend" / "static"),
)
# Local-only app; session signing key comes from the environment when set,
# otherwise a fresh random key is generated per process.
app.secret_key = os.environ.get("AETHERSCOPE_SECRET_KEY") or secrets.token_hex(32)
# Same-origin only by default. To run behind a reverse proxy on a different
# hostname, set AETHERSCOPE_ALLOWED_ORIGINS to a comma-separated list of the
# exact origins the browser uses, e.g. "https://aetherscope.example.com".
_allowed_origins = [
    o.strip() for o in os.environ.get("AETHERSCOPE_ALLOWED_ORIGINS", "").split(",") if o.strip()
]
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=_allowed_origins)

# Server-startup timestamp used to bust browser caches whenever the
# service restarts (so users can't get stuck on stale JS/CSS).
APP_VERSION = str(int(time.time()))


@app.context_processor
def _inject_version():
    return {"app_version": APP_VERSION}


@app.after_request
def _response_headers(resp):
    # Static assets are tiny and we always want the newest version after
    # a service restart. Tell browsers not to keep them.
    if resp.headers.get("Content-Type", "").startswith(("application/javascript", "text/css")):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    # All front-end deps are vendored locally, so the CSP can be strict.
    # Map tiles are the only external resource (cartocdn, img only).
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://*.basemaps.cartocdn.com; "
        "connect-src 'self'; "
        "worker-src 'self' blob:; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp

DEVICE_POLL_INTERVAL = 2.5

# Reentrant: callers nest with-blocks (e.g., _start_sweep calls _stop_all_locked
# while already holding the lock).
_state_lock = threading.RLock()

# Monotonic generation counter — bumped on every successful job start.
# on_exit callbacks check this to ignore stale completions from jobs
# that have already been superseded.
_current_job_gen = 0

_state: dict = {
    "mode": "idle",             # "idle" | "sweep" | "decode" | "capture" | "adsb" | "scan" | "radio"
    "streamer": None,
    "decoder": None,
    "capture": None,
    "adsb": None,
    "scanner": None,
    "radio": None,
    "scan_radio": None,
    "replay": None,
    "sweep_config": SweepConfig(),
    "decode_config": DecodeConfig(),
    "capture_config": CaptureConfig(),
    "adsb_config": AdsbConfig(),
    "scan_config": ScanConfig(),
    "radio_config": RadioConfig(),
}
_device: dict = {"info": None, "checked_at": 0.0}


# ------------------------------------------------------------------
# Emitters
# ------------------------------------------------------------------

# Sweep rate-limit. hackrf_sweep on narrow bands can produce hundreds
# of full sweeps per second. Without throttling we chunk-emit 4+ MB/s
# of JSON over the websocket and the browser tab becomes unresponsive
# (page loads stall, clicks lag). We render at most _SWEEP_EMIT_HZ
# events per second to the wire, while counting all sweeps for the
# rate display so the user sees the true device rate.
_SWEEP_EMIT_HZ = 30.0
_SWEEP_MIN_DT = 1.0 / _SWEEP_EMIT_HZ
_sweep_emit_lock = threading.Lock()
_sweep_last_emit = 0.0
_sweep_recent: list[float] = []


def _emit_sweep(freqs: np.ndarray, powers: np.ndarray) -> None:
    global _sweep_last_emit
    telemetry.bump("sweeps_computed")
    now = time.time()
    with _sweep_emit_lock:
        _sweep_recent.append(now)
        # window for rate calc: 2 seconds
        cutoff = now - 2.0
        while _sweep_recent and _sweep_recent[0] < cutoff:
            _sweep_recent.pop(0)
        rate_hz = len(_sweep_recent) / 2.0

        if now - _sweep_last_emit < _SWEEP_MIN_DT:
            return
        _sweep_last_emit = now

    telemetry.bump("sweeps_emitted")
    socketio.emit(
        "sweep",
        {
            "f0": float(freqs[0]),
            "f1": float(freqs[-1]),
            "bin_width": float(freqs[1] - freqs[0]) if len(freqs) > 1 else 0.0,
            "powers": powers.tolist(),
            "rate_hz": rate_hz,
        },
    )


def _emit_event(event: dict) -> None:
    socketio.emit("decoded", event)


def _emit_status() -> None:
    with _state_lock:
        snapshot = {
            "mode": _state["mode"],
            "sweep_config": asdict(_state["sweep_config"]),
            "decode_config": asdict(_state["decode_config"]),
            "capture_config": asdict(_state["capture_config"]),
            "adsb_config": asdict(_state["adsb_config"]),
            "radio_config": asdict(_state["radio_config"]),
            "scan_config": _state["scan_config"].__dict__,
        }
    socketio.emit("status", snapshot)


def _emit_device_status() -> None:
    socketio.emit(
        "device_status",
        {"info": _device["info"], "checked_at": _device["checked_at"]},
    )


def _emit_toast(level: str, message: str) -> None:
    socketio.emit("toast", {"level": level, "message": message})


def _emit_capture_progress(bytes_written: int, expected: int) -> None:
    pct = (bytes_written / expected * 100) if expected > 0 else 0
    socketio.emit("capture_progress", {
        "bytes_written": bytes_written,
        "expected": expected,
        "pct": pct,
    })


def _emit_captures_list() -> None:
    socketio.emit("captures", {"items": list_captures()})


def _emit_adsb(aircraft: list[dict], meta: dict) -> None:
    socketio.emit("adsb", {"aircraft": aircraft, "meta": meta})


def _emit_scan(name: str, payload: dict) -> None:
    socketio.emit(name, payload)


# ------------------------------------------------------------------
# Job lifecycle — all locked
# ------------------------------------------------------------------

def _stop_all_locked() -> None:
    """Snapshot all active jobs, clear them from _state, broadcast 'idle',
    then synchronously tear down subprocesses. Caller MUST hold _state_lock.
    """
    jobs = []
    for slot in ("streamer", "decoder", "capture", "adsb", "scanner", "radio", "scan_radio", "replay"):
        if _state[slot]:
            jobs.append((slot, _state[slot]))

    was_scanning = _state["mode"] == "scan"

    for slot in ("streamer", "decoder", "capture", "adsb", "scanner", "radio", "scan_radio", "replay"):
        _state[slot] = None
    _state["mode"] = "idle"

    # UI gets immediate feedback while subprocess termination happens after.
    _emit_status()
    if was_scanning:
        socketio.emit("scan_stopped", {})

    for name, job in jobs:
        try:
            if hasattr(job, "cancel"):
                job.cancel()
            else:
                job.stop()
        except Exception:
            log.exception("teardown of %s failed", name)


def _next_gen_locked() -> int:
    """Caller MUST hold _state_lock. Returns a fresh generation id."""
    global _current_job_gen
    _current_job_gen += 1
    return _current_job_gen


def _make_exit_handler(slot: str, gen: int, label: str):
    """Returns an on_exit callback that only acts if it's still the
    current generation. Avoids clobbering state when a stale subprocess
    finishes its teardown after a new job has already taken its slot.
    """
    def on_exit(reason: str) -> None:
        if reason == "stopped":
            return  # we initiated this, state is already correct
        # "died" — subprocess exited unexpectedly
        with _state_lock:
            if gen != _current_job_gen:
                return  # superseded
            _state[slot] = None
            _state["mode"] = "idle"
        telemetry.bump("subprocess_deaths")
        # If the device is gone, the disconnect toast covers this.
        if _device["info"] is not None:
            _emit_toast("error", f"{label} stopped unexpectedly.")
        _emit_status()
    return on_exit


def _start_sweep(cfg: SweepConfig) -> bool:
    if _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected.")
        return False
    with _state_lock:
        _stop_all_locked()
        gen = _next_gen_locked()
        _state["sweep_config"] = cfg
        streamer = SweepStreamer(
            cfg,
            on_sweep=_emit_sweep,
            on_exit=_make_exit_handler("streamer", gen, "Sweep"),
        )
        _state["streamer"] = streamer
        _state["mode"] = "sweep"
        streamer.start()
    _emit_status()
    return True


def _start_decode(cfg: DecodeConfig) -> bool:
    if _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected.")
        return False
    with _state_lock:
        _stop_all_locked()
        gen = _next_gen_locked()
        _state["decode_config"] = cfg
        decoder = Rtl433Decoder(
            cfg,
            on_event=_emit_event,
            on_exit=_make_exit_handler("decoder", gen, "Decoder"),
        )
        _state["decoder"] = decoder
        _state["mode"] = "decode"
        decoder.start()
    _emit_status()
    return True


def _start_capture(cfg: CaptureConfig) -> bool:
    if _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected.")
        return False
    with _state_lock:
        _stop_all_locked()
        gen = _next_gen_locked()
        _state["capture_config"] = cfg

        def on_done(record, reason: str) -> None:
            with _state_lock:
                # Only act if we're still the current capture
                if gen == _current_job_gen and _state["capture"] is not None:
                    _state["capture"] = None
                    if _state["mode"] == "capture":
                        _state["mode"] = "idle"
                        emit_status_after = True
                    else:
                        emit_status_after = False
                else:
                    emit_status_after = False
            if reason == "completed":
                size_mb = record.file_size / (1024 * 1024)
                _emit_toast("info", f"Capture done: {record.name} ({size_mb:.1f} MB)")
            elif reason == "cancelled":
                _emit_toast("warn", f"Capture cancelled: {record.name}")
            else:
                if _device["info"] is not None:
                    _emit_toast("error", f"Capture failed: {record.name}")
            socketio.emit("capture_done", {"record": asdict(record), "reason": reason})
            _emit_captures_list()
            if emit_status_after:
                _emit_status()

        cap = IqCapture(cfg, on_progress=_emit_capture_progress, on_done=on_done)
        record = cap.start()
        _state["capture"] = cap
        _state["mode"] = "capture"
        socketio.emit("capture_started", {"record": asdict(record)})
    _emit_status()
    return True


def _start_adsb(cfg: AdsbConfig) -> bool:
    if _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected.")
        return False
    with _state_lock:
        _stop_all_locked()
        gen = _next_gen_locked()
        _state["adsb_config"] = cfg
        recv = AdsbReceiver(
            cfg,
            on_update=_emit_adsb,
            on_exit=_make_exit_handler("adsb", gen, "ADS-B"),
        )
        _state["adsb"] = recv
        _state["mode"] = "adsb"
        recv.start()
    _emit_status()
    return True


def _start_radio(cfg: RadioConfig) -> bool:
    if _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected.")
        return False
    with _state_lock:
        _stop_all_locked()
        gen = _next_gen_locked()
        _state["radio_config"] = cfg
        recv = RadioReceiver(
            cfg,
            on_audio=lambda pcm: socketio.emit("radio_audio", pcm),
            on_exit=_make_exit_handler("radio", gen, "Radio"),
            on_signal=lambda db: socketio.emit("radio_signal", {"dbfs": db}),
        )
        _state["radio"] = recv
        _state["mode"] = "radio"
        recv.start()
    socketio.emit(
        "radio_started",
        {"sample_rate": AUDIO_RATE, "freq_mhz": cfg.freq_mhz, "demod": cfg.demod},
    )
    _emit_status()
    return True


def _start_scan_radio(cfg: ScanRadioConfig) -> bool:
    if _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected.")
        return False
    if not cfg.freqs_mhz:
        _emit_toast("error", "No marked frequencies to scan.")
        return False
    with _state_lock:
        _stop_all_locked()
        gen = _next_gen_locked()
        scanner = RadioScanner(
            cfg,
            on_audio=lambda pcm: socketio.emit("radio_audio", pcm),
            on_event=lambda name, payload: socketio.emit(name, payload),
            on_exit=_make_exit_handler("scan_radio", gen, "Scanner"),
        )
        _state["scan_radio"] = scanner
        _state["mode"] = "scan_radio"
        scanner.start()
    socketio.emit("scan_radio_started",
                  {"count": len(cfg.freqs_mhz), "sample_rate": AUDIO_RATE, "demod": cfg.demod})
    _emit_status()
    return True


def _start_replay(name: str) -> bool:
    if "/" in name or ".." in name:
        _emit_toast("error", "Invalid capture name")
        return False
    match = next((c for c in list_captures() if c.get("name") == name), None)
    if not match or not match.get("path") or not os.path.exists(match["path"]):
        _emit_toast("error", "Capture not found")
        return False
    center = int(match.get("freq_hz") or 0)
    sr = int(match.get("sample_rate") or 2_000_000)
    with _state_lock:
        _stop_all_locked()
        gen = _next_gen_locked()

        def on_done(reason: str) -> None:
            with _state_lock:
                if gen == _current_job_gen and _state["replay"] is not None:
                    _state["replay"] = None
                    if _state["mode"] == "replay":
                        _state["mode"] = "idle"
            socketio.emit("replay_done", {"reason": reason, "name": name})
            _emit_status()

        rp = IqReplay(
            match["path"], center, sr,
            on_frame=lambda f0, f1, powers: socketio.emit(
                "sweep", {"f0": f0, "f1": f1, "powers": powers, "rate_hz": 20.0}),
            on_done=on_done,
        )
        _state["replay"] = rp
        _state["mode"] = "replay"
        rp.start()
    socketio.emit("replay_started", {"name": name, "freq_hz": center, "sample_rate": sr})
    _emit_status()
    return True


def _start_scan(cfg: ScanConfig) -> bool:
    if _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected.")
        return False
    with _state_lock:
        _stop_all_locked()
        gen = _next_gen_locked()
        _state["scan_config"] = cfg

        def on_scan_event(name: str, payload: dict) -> None:
            if name in ("scan_completed", "scan_failed"):
                with _state_lock:
                    if gen == _current_job_gen and _state["scanner"] is not None:
                        _state["scanner"] = None
                        if _state["mode"] == "scan":
                            _state["mode"] = "idle"
                            emit_after = True
                        else:
                            emit_after = False
                    else:
                        emit_after = False
                _emit_scan(name, payload)
                if emit_after:
                    _emit_status()
            else:
                _emit_scan(name, payload)

        scanner = AutoScanner(cfg, on_event=on_scan_event)
        _state["scanner"] = scanner
        _state["mode"] = "scan"
        scanner.start()
    _emit_status()
    return True


def _stop_all_external() -> None:
    """Public stop entry point: lock + tear down."""
    with _state_lock:
        _stop_all_locked()


# ------------------------------------------------------------------
# Background device poller
# ------------------------------------------------------------------

_SENTINEL = object()


def _device_poller() -> None:
    last_serial: object = _SENTINEL
    while True:
        socketio.emit("telemetry", telemetry.snapshot())
        with _state_lock:
            busy = _state["mode"] != "idle"
        if busy:
            # A job owns the HackRF; skip the probe so hackrf_info doesn't
            # contend with the running subprocess. Assume the device is present.
            time.sleep(DEVICE_POLL_INTERVAL)
            continue
        try:
            info = probe_hackrf()
        except Exception:
            log.exception("device poll failed")
            info = None
        _device["info"] = info
        _device["checked_at"] = time.time()
        _emit_device_status()

        new_serial = info.get("serial") if info else None
        if last_serial is not _SENTINEL and new_serial != last_serial:
            if info:
                tail = info.get("serial", "?")[-6:].upper()
                _emit_toast("info", f"HackRF connected ({tail})")
            else:
                _emit_toast("warn", "HackRF disconnected")
        last_serial = new_serial
        time.sleep(DEVICE_POLL_INTERVAL)


# ------------------------------------------------------------------
# Shutdown handling
# ------------------------------------------------------------------

_shutdown_done = threading.Event()


def _shutdown(*_args) -> None:
    """Stop all subprocesses on app exit. Safe to call multiple times."""
    if _shutdown_done.is_set():
        return
    _shutdown_done.set()
    log.info("shutting down — stopping any active jobs")
    try:
        _stop_all_external()
    except Exception:
        log.exception("error during shutdown cleanup")


# ------------------------------------------------------------------
# Routes & socket events
# ------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/captures/<path:name>")
def serve_capture(name: str):
    return send_from_directory(CAPTURES_DIR, name, as_attachment=True)


@app.route("/api/captures")
def api_captures():
    return jsonify(list_captures())


@socketio.on("connect")
def on_connect():
    with _state_lock:
        snapshot = {
            "mode": _state["mode"],
            "sweep_config": asdict(_state["sweep_config"]),
            "decode_config": asdict(_state["decode_config"]),
            "capture_config": asdict(_state["capture_config"]),
            "adsb_config": asdict(_state["adsb_config"]),
            "radio_config": asdict(_state["radio_config"]),
            "scan_config": _state["scan_config"].__dict__,
        }
    emit("status", snapshot)
    emit("device_status", {"info": _device["info"], "checked_at": _device["checked_at"]})
    emit("captures", {"items": list_captures()})


def _filter_payload(data, cls):
    keys = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return {k: v for k, v in (data or {}).items() if k in keys}


@socketio.on("start_sweep")
def on_start_sweep(data):
    try:
        cfg = SweepConfig(**_filter_payload(data, SweepConfig))
    except (TypeError, ValueError) as e:
        _emit_toast("error", f"Invalid sweep config: {e}")
        return
    _start_sweep(cfg)


@socketio.on("start_decode")
def on_start_decode(data):
    try:
        cfg = DecodeConfig(**_filter_payload(data, DecodeConfig))
    except (TypeError, ValueError) as e:
        _emit_toast("error", f"Invalid decode config: {e}")
        return
    _start_decode(cfg)


@socketio.on("start_capture")
def on_start_capture(data):
    try:
        cfg = CaptureConfig(**_filter_payload(data, CaptureConfig))
    except (TypeError, ValueError) as e:
        _emit_toast("error", f"Invalid capture config: {e}")
        return
    err = capture_config_error(cfg)
    if err:
        _emit_toast("error", err)
        return
    _start_capture(cfg)


@socketio.on("start_adsb")
def on_start_adsb(data):
    try:
        cfg = AdsbConfig(**_filter_payload(data, AdsbConfig))
    except (TypeError, ValueError) as e:
        _emit_toast("error", f"Invalid ADS-B config: {e}")
        return
    _start_adsb(cfg)


@socketio.on("start_radio")
def on_start_radio(data):
    try:
        cfg = RadioConfig(**_filter_payload(data, RadioConfig))
    except (TypeError, ValueError) as e:
        _emit_toast("error", f"Invalid radio config: {e}")
        return
    if cfg.demod not in ("fm", "nfm", "am"):
        _emit_toast("error", "Invalid demod (use fm, nfm, or am)")
        return
    if not (1.0 <= cfg.freq_mhz <= 6000.0):
        _emit_toast("error", "Frequency out of range (1-6000 MHz)")
        return
    _start_radio(cfg)


@socketio.on("start_scan_radio")
def on_start_scan_radio(data):
    data = data or {}
    demod = data.get("demod", "nfm")
    if demod not in ("fm", "nfm", "am"):
        _emit_toast("error", "Invalid demod (use fm, nfm, or am)")
        return
    freqs = []
    for f in (data.get("freqs") or []):
        try:
            mhz = float(f)
        except (TypeError, ValueError):
            continue
        if 1.0 <= mhz <= 6000.0:
            freqs.append(mhz)
    if not freqs:
        _emit_toast("error", "No valid frequencies to scan")
        return
    try:
        squelch = float(data.get("squelch_dbfs", -45.0))
    except (TypeError, ValueError):
        squelch = -45.0
    _start_scan_radio(ScanRadioConfig(freqs_mhz=freqs, demod=demod, squelch_dbfs=squelch))


@socketio.on("start_replay")
def on_start_replay(data):
    name = (data or {}).get("name", "")
    if name:
        _start_replay(name)


@socketio.on("start_scan")
def on_start_scan(data):
    try:
        cfg = ScanConfig(**_filter_payload(data, ScanConfig))
    except (TypeError, ValueError) as e:
        _emit_toast("error", f"Invalid scan config: {e}")
        return
    _start_scan(cfg)


@socketio.on("cancel_capture")
def on_cancel_capture():
    with _state_lock:
        if _state["capture"]:
            try:
                _state["capture"].cancel()
            except Exception:
                log.exception("cancel_capture failed")


@socketio.on("stop")
def on_stop():
    _stop_all_external()


@socketio.on("refresh_device")
def on_refresh_device():
    try:
        _device["info"] = probe_hackrf()
        _device["checked_at"] = time.time()
    except Exception:
        log.exception("manual device refresh failed")
        _device["info"] = None
    _emit_device_status()


@socketio.on("list_captures")
def on_list_captures():
    _emit_captures_list()


@socketio.on("delete_capture")
def on_delete_capture(data):
    name = (data or {}).get("name", "")
    if delete_capture(name):
        _emit_toast("info", f"Deleted {name}")
    _emit_captures_list()


def main() -> None:
    # Bind localhost by default; containers set AETHERSCOPE_HOST=0.0.0.0 and
    # rely on `-p 127.0.0.1:8765:8765` (and a reverse proxy) for exposure.
    host = os.environ.get("AETHERSCOPE_HOST", "127.0.0.1")
    log.info("Aetherscope listening on http://%s:8765", host)
    # Register cleanup for graceful shutdown so we don't orphan subprocesses
    # holding the HackRF when launchd sends SIGTERM.
    atexit.register(_shutdown)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, lambda *_: (_shutdown(), sys.exit(0)))
        except (OSError, ValueError):
            pass  # not running on main thread; atexit still covers most cases
    threading.Thread(target=_device_poller, daemon=True).start()
    socketio.run(app, host=host, port=8765, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
