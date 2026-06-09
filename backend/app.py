"""Flask + Socket.IO server.

Streams HackRF sweep rows and rtl_433 decoded events to the browser.
Sweep and decode are mutually exclusive (HackRF can only be claimed
by one process at a time). Background poller emits device_status so
the UI knows whether the HackRF is plugged in.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

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

DEVICE_POLL_INTERVAL = 2.5  # seconds

_state: dict = {
    "mode": "idle",             # "idle" | "sweep" | "decode"
    "streamer": None,
    "decoder": None,
    "sweep_config": SweepConfig(),
    "decode_config": DecodeConfig(),
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
        },
    )


def _emit_device_status() -> None:
    socketio.emit(
        "device_status",
        {
            "info": _device["info"],
            "checked_at": _device["checked_at"],
        },
    )


def _emit_toast(level: str, message: str) -> None:
    """level: 'info' | 'warn' | 'error'"""
    socketio.emit("toast", {"level": level, "message": message})


# ------------------------------------------------------------------
# Job control (sweep & decode are mutually exclusive)
# ------------------------------------------------------------------

def _stop_all() -> None:
    if _state["streamer"]:
        _state["streamer"].stop()
        _state["streamer"] = None
    if _state["decoder"]:
        _state["decoder"].stop()
        _state["decoder"] = None
    _state["mode"] = "idle"


def _on_sweep_exit(reason: str) -> None:
    if reason == "died":
        log.warning("sweep subprocess died unexpectedly")
        _emit_toast("error", "Sweep stopped: hackrf_sweep exited unexpectedly. Is the HackRF still connected?")
    _state["streamer"] = None
    if _state["mode"] == "sweep":
        _state["mode"] = "idle"
        _emit_status()


def _on_decode_exit(reason: str) -> None:
    if reason == "died":
        log.warning("decoder subprocess died unexpectedly")
        _emit_toast("error", "Decoder stopped: rtl_433 exited unexpectedly. Is the HackRF still connected?")
    _state["decoder"] = None
    if _state["mode"] == "decode":
        _state["mode"] = "idle"
        _emit_status()


def _start_sweep(cfg: SweepConfig) -> bool:
    if _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected. Plug it in and refresh device.")
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
        _emit_toast("error", "Cannot start: HackRF not detected. Plug it in and refresh device.")
        return False
    _stop_all()
    _state["decode_config"] = cfg
    decoder = Rtl433Decoder(cfg, on_event=_emit_event, on_exit=_on_decode_exit)
    decoder.start()
    _state["decoder"] = decoder
    _state["mode"] = "decode"
    return True


# ------------------------------------------------------------------
# Background device poller
# ------------------------------------------------------------------

def _device_poller() -> None:
    last_serial = object()
    while True:
        try:
            info = probe_hackrf()
        except Exception:
            log.exception("device poll failed")
            info = None
        _device["info"] = info
        _device["checked_at"] = time.time()
        # always emit so the UI knows we're alive
        _emit_device_status()
        # toast on transition
        new_serial = info.get("serial") if info else None
        if last_serial is not object() and new_serial != last_serial:
            if info:
                _emit_toast("info", f"HackRF connected ({info.get('serial', '?')[:8]})")
            else:
                _emit_toast("warn", "HackRF disconnected")
        last_serial = new_serial
        time.sleep(DEVICE_POLL_INTERVAL)


# ------------------------------------------------------------------
# Routes & events
# ------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("connect")
def on_connect():
    emit(
        "status",
        {
            "mode": _state["mode"],
            "sweep_config": asdict(_state["sweep_config"]),
            "decode_config": asdict(_state["decode_config"]),
        },
    )
    emit(
        "device_status",
        {"info": _device["info"], "checked_at": _device["checked_at"]},
    )


@socketio.on("start_sweep")
def on_start_sweep(data):
    keys = {f.name for f in SweepConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    payload = {k: v for k, v in (data or {}).items() if k in keys}
    if _start_sweep(SweepConfig(**payload)):
        _emit_status()


@socketio.on("start_decode")
def on_start_decode(data):
    keys = {f.name for f in DecodeConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    payload = {k: v for k, v in (data or {}).items() if k in keys}
    if _start_decode(DecodeConfig(**payload)):
        _emit_status()


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
        log.exception("manual device refresh failed")
        _device["info"] = None
    _emit_device_status()


def main() -> None:
    log.info("hackrf-web listening on http://127.0.0.1:8765")
    threading.Thread(target=_device_poller, daemon=True).start()
    socketio.run(app, host="127.0.0.1", port=8765, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
