"""Flask + Socket.IO server. Streams HackRF sweep rows and rtl_433
decoded events to the browser. Sweep and decode are mutually exclusive
because the HackRF can only be claimed by one process at a time.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

import numpy as np
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

from .decoders import DecodeConfig, Rtl433Decoder
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

_state: dict = {
    "mode": "idle",             # "idle" | "sweep" | "decode"
    "streamer": None,
    "decoder": None,
    "sweep_config": SweepConfig(),
    "decode_config": DecodeConfig(),
}


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


def _start_sweep(cfg: SweepConfig) -> None:
    _stop_all()
    _state["sweep_config"] = cfg
    streamer = SweepStreamer(cfg, on_sweep=_emit_sweep)
    streamer.start()
    _state["streamer"] = streamer
    _state["mode"] = "sweep"


def _start_decode(cfg: DecodeConfig) -> None:
    _stop_all()
    _state["decode_config"] = cfg
    decoder = Rtl433Decoder(cfg, on_event=_emit_event)
    decoder.start()
    _state["decoder"] = decoder
    _state["mode"] = "decode"


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


@socketio.on("start_sweep")
def on_start_sweep(data):
    keys = {f.name for f in SweepConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    payload = {k: v for k, v in (data or {}).items() if k in keys}
    _start_sweep(SweepConfig(**payload))
    _emit_status()


@socketio.on("start_decode")
def on_start_decode(data):
    keys = {f.name for f in DecodeConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    payload = {k: v for k, v in (data or {}).items() if k in keys}
    _start_decode(DecodeConfig(**payload))
    _emit_status()


@socketio.on("stop")
def on_stop():
    _stop_all()
    _emit_status()


def main() -> None:
    log.info("hackrf-web listening on http://127.0.0.1:8765")
    socketio.run(app, host="127.0.0.1", port=8765, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
