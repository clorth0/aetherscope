"""Flask + Socket.IO server. Streams HackRF sweep rows to the browser."""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

import numpy as np
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

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

_state: dict = {"streamer": None, "config": SweepConfig()}


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


def _start_streamer(cfg: SweepConfig) -> None:
    if _state["streamer"]:
        _state["streamer"].stop()
    _state["config"] = cfg
    streamer = SweepStreamer(cfg, on_sweep=_emit_sweep)
    streamer.start()
    _state["streamer"] = streamer


def _stop_streamer() -> None:
    if _state["streamer"]:
        _state["streamer"].stop()
        _state["streamer"] = None


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("connect")
def on_connect():
    emit(
        "status",
        {
            "running": bool(_state["streamer"] and _state["streamer"].is_running()),
            "config": asdict(_state["config"]),
        },
    )


@socketio.on("start")
def on_start(data):
    keys = {f.name for f in SweepConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    payload = {k: v for k, v in (data or {}).items() if k in keys}
    cfg = SweepConfig(**payload)
    _start_streamer(cfg)
    emit("status", {"running": True, "config": asdict(cfg)}, broadcast=True)


@socketio.on("stop")
def on_stop():
    _stop_streamer()
    emit("status", {"running": False, "config": asdict(_state["config"])}, broadcast=True)


def main() -> None:
    log.info("hackrf-web listening on http://127.0.0.1:8765")
    socketio.run(app, host="127.0.0.1", port=8765, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
