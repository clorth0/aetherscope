"""Flask + Socket.IO server.

Streams HackRF sweep rows, rtl_433 events, and IQ captures. All three
operations require exclusive access to the HackRF and are mutually
exclusive. A background poller emits device_status so the UI knows
whether a HackRF is plugged in.
"""

from __future__ import annotations

import logging
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

DEVICE_POLL_INTERVAL = 2.5

_state: dict = {
    "mode": "idle",             # "idle" | "sweep" | "decode" | "capture" | "adsb"
    "streamer": None,
    "decoder": None,
    "capture": None,
    "adsb": None,
    "sweep_config": SweepConfig(),
    "decode_config": DecodeConfig(),
    "capture_config": CaptureConfig(),
    "adsb_config": AdsbConfig(),
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
    socketio.emit(
        "status",
        {
            "mode": _state["mode"],
            "sweep_config": asdict(_state["sweep_config"]),
            "decode_config": asdict(_state["decode_config"]),
            "capture_config": asdict(_state["capture_config"]),
            "adsb_config": asdict(_state["adsb_config"]),
        },
    )


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


# ------------------------------------------------------------------
# Job control
# ------------------------------------------------------------------

def _stop_all() -> None:
    if _state["streamer"]:
        _state["streamer"].stop()
        _state["streamer"] = None
    if _state["decoder"]:
        _state["decoder"].stop()
        _state["decoder"] = None
    if _state["capture"]:
        _state["capture"].cancel()
        _state["capture"] = None
    if _state["adsb"]:
        _state["adsb"].stop()
        _state["adsb"] = None
    _state["mode"] = "idle"


def _on_sweep_exit(reason: str) -> None:
    if reason == "died":
        _emit_toast("error", "Sweep stopped: hackrf_sweep exited unexpectedly. Is the HackRF still connected?")
    _state["streamer"] = None
    if _state["mode"] == "sweep":
        _state["mode"] = "idle"
        _emit_status()


def _on_decode_exit(reason: str) -> None:
    if reason == "died":
        _emit_toast("error", "Decoder stopped: rtl_433 exited unexpectedly. Is the HackRF still connected?")
    _state["decoder"] = None
    if _state["mode"] == "decode":
        _state["mode"] = "idle"
        _emit_status()


def _on_capture_done(record, reason: str) -> None:
    _state["capture"] = None
    if _state["mode"] == "capture":
        _state["mode"] = "idle"
        _emit_status()
    if reason == "completed":
        size_mb = record.file_size / (1024 * 1024)
        _emit_toast("info", f"Capture done: {record.name} ({size_mb:.1f} MB)")
    elif reason == "cancelled":
        _emit_toast("warn", f"Capture cancelled: {record.name}")
    else:  # died
        _emit_toast("error", f"Capture failed: {record.name} (HackRF disconnected?)")
    socketio.emit("capture_done", {"record": asdict(record), "reason": reason})
    _emit_captures_list()


def _start_sweep(cfg: SweepConfig) -> bool:
    if _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected.")
        return False
    _stop_all()
    _state["sweep_config"] = cfg
    streamer = SweepStreamer(cfg, on_sweep=_emit_sweep, on_exit=_on_sweep_exit)
    streamer.start()
    _state["streamer"] = streamer
    _state["mode"] = "sweep"
    return True


def _start_decode(cfg: DecodeConfig) -> bool:
    if _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected.")
        return False
    _stop_all()
    _state["decode_config"] = cfg
    decoder = Rtl433Decoder(cfg, on_event=_emit_event, on_exit=_on_decode_exit)
    decoder.start()
    _state["decoder"] = decoder
    _state["mode"] = "decode"
    return True


def _on_adsb_exit(reason: str) -> None:
    if reason == "died":
        _emit_toast("error", "ADS-B stopped: readsb exited unexpectedly. Is the HackRF still connected?")
    _state["adsb"] = None
    if _state["mode"] == "adsb":
        _state["mode"] = "idle"
        _emit_status()


def _start_adsb(cfg: AdsbConfig) -> bool:
    if _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected.")
        return False
    _stop_all()
    _state["adsb_config"] = cfg
    recv = AdsbReceiver(cfg, on_update=_emit_adsb, on_exit=_on_adsb_exit)
    recv.start()
    _state["adsb"] = recv
    _state["mode"] = "adsb"
    return True


def _start_capture(cfg: CaptureConfig) -> bool:
    if _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected.")
        return False
    _stop_all()
    _state["capture_config"] = cfg
    cap = IqCapture(cfg, on_progress=_emit_capture_progress, on_done=_on_capture_done)
    record = cap.start()
    _state["capture"] = cap
    _state["mode"] = "capture"
    socketio.emit("capture_started", {"record": asdict(record)})
    return True


# ------------------------------------------------------------------
# Background device poller
# ------------------------------------------------------------------

def _device_poller() -> None:
    last_serial: object = object()
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
        if last_serial is not object and new_serial != last_serial:
            if info:
                _emit_toast("info", f"HackRF connected ({info.get('serial', '?')[-6:].upper()})")
            else:
                _emit_toast("warn", "HackRF disconnected")
        last_serial = new_serial
        time.sleep(DEVICE_POLL_INTERVAL)


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
    emit("status", {
        "mode": _state["mode"],
        "sweep_config": asdict(_state["sweep_config"]),
        "decode_config": asdict(_state["decode_config"]),
        "capture_config": asdict(_state["capture_config"]),
        "adsb_config": asdict(_state["adsb_config"]),
    })
    emit("device_status", {"info": _device["info"], "checked_at": _device["checked_at"]})
    emit("captures", {"items": list_captures()})


def _filter_payload(data, cls):
    keys = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return {k: v for k, v in (data or {}).items() if k in keys}


@socketio.on("start_sweep")
def on_start_sweep(data):
    if _start_sweep(SweepConfig(**_filter_payload(data, SweepConfig))):
        _emit_status()


@socketio.on("start_decode")
def on_start_decode(data):
    if _start_decode(DecodeConfig(**_filter_payload(data, DecodeConfig))):
        _emit_status()


@socketio.on("start_capture")
def on_start_capture(data):
    if _start_capture(CaptureConfig(**_filter_payload(data, CaptureConfig))):
        _emit_status()


@socketio.on("start_adsb")
def on_start_adsb(data):
    if _start_adsb(AdsbConfig(**_filter_payload(data, AdsbConfig))):
        _emit_status()


@socketio.on("cancel_capture")
def on_cancel_capture():
    if _state["capture"]:
        _state["capture"].cancel()


@socketio.on("stop")
def on_stop():
    _stop_all()
    _emit_status()


@socketio.on("refresh_device")
def on_refresh_device():
    try:
        _device["info"] = probe_hackrf()
        _device["checked_at"] = time.time()
    except Exception:
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
    threading.Thread(target=_device_poller, daemon=True).start()
    socketio.run(app, host="127.0.0.1", port=8765, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
