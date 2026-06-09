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
from .capture import CaptureConfig, IqCapture, CAPTURES_DIR, delete_capture, list_captures
from .decoders import DecodeConfig, Rtl433Decoder
from .device import probe_hackrf
from .scan import AutoScanner, ScanConfig
from .sdr import SweepConfig, SweepStreamer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hackrf-web")

ROOT = Path(__file__).resolve().parent.parent
app = Flask(
    __name__,
    template_folder=str(ROOT / "frontend" / "templates"),
    static_folder=str(ROOT / "frontend" / "static"),
)
app.config["SECRET_KEY"] = "dev-local-only"
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=[])

# Server-startup timestamp used to bust browser caches whenever the
# service restarts (so users can't get stuck on stale JS/CSS).
APP_VERSION = str(int(time.time()))


@app.context_processor
def _inject_version():
    return {"app_version": APP_VERSION}


@app.after_request
def _no_cache_static(resp):
    # Static assets are tiny and we always want the newest version after
    # a service restart. Tell browsers not to keep them.
    if resp.headers.get("Content-Type", "").startswith(("application/javascript", "text/css")):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
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
    "mode": "idle",             # "idle" | "sweep" | "decode" | "capture" | "adsb" | "scan"
    "streamer": None,
    "decoder": None,
    "capture": None,
    "adsb": None,
    "scanner": None,
    "sweep_config": SweepConfig(),
    "decode_config": DecodeConfig(),
    "capture_config": CaptureConfig(),
    "adsb_config": AdsbConfig(),
    "scan_config": ScanConfig(),
}
_device: dict = {"info": None, "checked_at": 0.0}


# ------------------------------------------------------------------
# Emitters
# ------------------------------------------------------------------

def _emit_sweep(freqs: np.ndarray, powers: np.ndarray) -> None:
    socketio.emit(
        "sweep",
        {
            "f0": float(freqs[0]),
            "f1": float(freqs[-1]),
            "bin_width": float(freqs[1] - freqs[0]) if len(freqs) > 1 else 0.0,
            "powers": powers.tolist(),
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
    for slot in ("streamer", "decoder", "capture", "adsb", "scanner"):
        if _state[slot]:
            jobs.append((slot, _state[slot]))

    was_scanning = _state["mode"] == "scan"

    for slot in ("streamer", "decoder", "capture", "adsb", "scanner"):
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
    _start_capture(cfg)


@socketio.on("start_adsb")
def on_start_adsb(data):
    try:
        cfg = AdsbConfig(**_filter_payload(data, AdsbConfig))
    except (TypeError, ValueError) as e:
        _emit_toast("error", f"Invalid ADS-B config: {e}")
        return
    _start_adsb(cfg)


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
    log.info("hackrf-web listening on http://127.0.0.1:8765")
    # Register cleanup for graceful shutdown so we don't orphan subprocesses
    # holding the HackRF when launchd sends SIGTERM.
    atexit.register(_shutdown)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, lambda *_: (_shutdown(), sys.exit(0)))
        except (OSError, ValueError):
            pass  # not running on main thread; atexit still covers most cases
    threading.Thread(target=_device_poller, daemon=True).start()
    socketio.run(app, host="127.0.0.1", port=8765, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
