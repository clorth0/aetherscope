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
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
from flask import Flask, jsonify, render_template, send_from_directory
from flask_socketio import SocketIO, emit

from .adsb import AdsbConfig, AdsbReceiver
from .adsb_enrich import icao_to_registration, icao_country
from .capture import CaptureConfig, IqCapture, CAPTURES_DIR, capture_basename, delete_capture, list_captures, redact_location
from .gps import GpsClient, coarsen_geolocation
from .audio_record import start_recording, finalize_recording
from .decoders import DecodeConfig, Rtl433Decoder
from .device import probe_hackrf
from .radio import AUDIO_RATE, RadioConfig, RadioReceiver, RadioScanner, ScanRadioConfig
from .tuning import find_peak_offset
from .replay import IqReplay
from .iq_playback import IqAudioPlayer
from .decode_file import IqFileDecoder
from .scan import AutoScanner, ScanConfig
from .sdr import SweepConfig, SweepStreamer
from .jobs import JobBoard, mode_spec
from .store import get_store
from . import telemetry

PRESETS = [
    {"freq_hz": 93_900_000,  "demod": "fm", "label": "93.9"},
    {"freq_hz": 97_100_000,  "demod": "fm", "label": "97.1"},
    {"freq_hz": 99_500_000,  "demod": "fm", "label": "99.5"},
    {"freq_hz": 101_100_000, "demod": "fm", "label": "101.1"},
    {"freq_hz": 105_900_000, "demod": "fm", "label": "105.9"},
    {"freq_hz": 107_600_000, "demod": "fm", "label": "107.6"},
    {"freq_hz": 118_300_000, "demod": "am", "label": "Air 118.3"},
    {"freq_hz": 121_500_000, "demod": "am", "label": "Air 121.5"},
    {"freq_hz": 124_000_000, "demod": "am", "label": "Air 124.0"},
]

_SETTING_ALLOWLIST = {"last_mode", "last_radio_freq", "last_demod", "radio_volume",
                      "gps_enabled", "gps_precision"}
_BOOKMARK_SOURCE_ALLOWLIST = {"user", "mark", "seed"}

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
def resolve_allowed_origins(raw: str | None) -> list[str] | None:
    if not raw or not raw.strip():
        return None
    origins = [part.strip() for part in raw.split(",") if part.strip()]
    return origins or None


_allowed_origins = resolve_allowed_origins(os.environ.get("AETHERSCOPE_ALLOWED_ORIGINS"))
# When unset, `None` makes engine.io enforce the request's own origin (same-origin
# only). An empty list would instead DISABLE the origin check, letting any page in
# the browser drive the SDR over the websocket, so default to None.
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
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp

DEVICE_POLL_INTERVAL = 2.5

# Reentrant: callers nest with-blocks (e.g., _start_sweep calls _stop_all_locked
# while already holding the lock).
_state_lock = threading.RLock()

# The only running job. Persisted configs stay in _state so the UI can
# restore the last sweep, radio, and so on while the board is idle.
_jobs = JobBoard()

_state: dict = {
    "sweep_config": SweepConfig(),
    "decode_config": DecodeConfig(),
    "capture_config": CaptureConfig(),
    "adsb_config": AdsbConfig(),
    "scan_config": ScanConfig(),
    "radio_config": RadioConfig(),
}
_device: dict = {"info": None, "checked_at": 0.0}

# Optional GPS geotagging (opt-in, default off). AETHERSCOPE_GPS=0 hard-disables
# it regardless of the stored toggle (deployments with no GPS).
_GPS_FORCED_OFF = os.environ.get("AETHERSCOPE_GPS") == "0"
_gps = GpsClient(on_status=lambda p: socketio.emit("gps_status", p))

# Active WAV recording of radio audio (None when not recording). Guarded by
# _state_lock; the recorder's own write() is thread-safe and a no-op after close.
_audio_rec: dict = {"recorder": None, "record": None}


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


_contact_write_ts: dict = {}     # key -> last DB write time (throttle)
_contact_emit_ts = [0.0]         # last 'contacts' broadcast time
_CONTACT_WRITE_DT = 2.0
_CONTACT_EMIT_DT = 3.0


def _emit_contacts() -> None:
    try:
        socketio.emit("contacts", {"data": get_store().list_contacts()})
    except Exception:
        log.exception("emit contacts failed")


def _record_contact(key, kind, label, ident="", info=None, lat=None, lon=None) -> None:
    """Throttled upsert of a contact sighting into the persistent inventory."""
    now = time.time()
    if now - _contact_write_ts.get(key, 0.0) < _CONTACT_WRITE_DT:
        return
    _contact_write_ts[key] = now
    try:
        get_store().record_contact(int(now), key, kind, label, ident, info, lat, lon)
    except Exception:
        log.exception("record_contact failed")
        return
    if now - _contact_emit_ts[0] >= _CONTACT_EMIT_DT:
        _contact_emit_ts[0] = now
        _emit_contacts()


def _receiver_geotag():
    """Coarsened receiver geolocation if GPS is enabled and has a fix, else None."""
    geo = _gps.geolocation()
    if geo is None:
        return None
    try:
        return coarsen_geolocation(geo, str(get_store().get_setting("gps_precision", "full")))
    except Exception:
        return None


def _emit_event(event: dict) -> None:
    socketio.emit("decoded", event)
    model = event.get("model")
    if not model:
        return
    ident = str(event.get("id", event.get("channel", "")))
    info = {k: event[k] for k in ("channel", "battery_ok", "temperature_C",
                                  "humidity", "freq", "rssi") if k in event}
    geo = _receiver_geotag()
    _record_contact(f"ism:{model}:{ident}", "ism", str(model), ident=ident, info=info,
                    lat=(geo or {}).get("lat"), lon=(geo or {}).get("lon"))


def _status_fields_locked() -> dict:
    return {
        "mode": _jobs.mode(),
        "status_label": _jobs.status_label(),
        "sweep_config": asdict(_state["sweep_config"]),
        "decode_config": asdict(_state["decode_config"]),
        "capture_config": asdict(_state["capture_config"]),
        "adsb_config": asdict(_state["adsb_config"]),
        "radio_config": asdict(_state["radio_config"]),
        "scan_config": _state["scan_config"].__dict__,
    }


def _emit_status() -> None:
    with _state_lock:
        snapshot = _status_fields_locked()
    socketio.emit("status", snapshot)


def _emit_device_status() -> None:
    socketio.emit(
        "device_status",
        {"info": _device["info"], "checked_at": _device["checked_at"]},
    )


def _emit_gps_status() -> None:
    socketio.emit("gps_status", _gps.position())


def _audio_status_snapshot() -> dict:
    with _state_lock:
        record = _audio_rec["record"]
        return {
            "recording": _audio_rec["recorder"] is not None,
            "name": record.name if record else None,
        }


def _emit_audio_record_status() -> None:
    socketio.emit("audio_record_status", _audio_status_snapshot())


def _on_radio_audio(pcm: bytes) -> None:
    """Radio audio sink: stream to the browser and tee into any active WAV."""
    socketio.emit("radio_audio", pcm)
    rec = _audio_rec["recorder"]   # lock-free read; write() is safe after close
    if rec is not None:
        rec.write(pcm)


def _finalize_audio_record_locked() -> None:
    """Finalize any active recording and refresh the UI. Caller holds _state_lock."""
    rec = _audio_rec["recorder"]
    record = _audio_rec["record"]
    if rec is None:
        return
    _audio_rec["recorder"] = None
    _audio_rec["record"] = None
    try:
        finalize_recording(rec, record, int(time.time()))
    except Exception:
        log.exception("finalize audio record failed")
    _emit_audio_record_status()
    _emit_captures_list()


def _emit_toast(level: str, message: str) -> None:
    socketio.emit("toast", {"level": level, "message": message})


def _emit_capture_progress(bytes_written: int, expected: int) -> None:
    pct = (bytes_written / expected * 100) if expected > 0 else 0
    socketio.emit("capture_progress", {
        "bytes_written": bytes_written,
        "expected": expected,
        "pct": pct,
    })


def _emit_bookmarks() -> None:
    socketio.emit("bookmarks", {"data": get_store().list_bookmarks()})


def _enriched_captures() -> list[dict]:
    """Return list_captures() items enriched with store annotations + missing flag."""
    items = list_captures()
    anns = get_store().list_capture_annotations()  # one query, dict lookup per item
    for item in items:
        name = item.get("name", "")
        item["kind"] = item.get("kind", "iq")   # audio recordings set kind="audio"
        ann = anns.get(name)
        item["user_label"] = ann["user_label"] if ann else ""
        item["notes"] = ann["notes"] if ann else ""
        item["tags"] = ann["tags"] if ann else []
        # Mark missing if the data file (.iq or .wav) no longer exists on disk
        data_path = CAPTURES_DIR / name if name else None
        item["missing"] = not (data_path and data_path.exists())
        # SigMF applies to IQ captures only, not audio.
        meta = CAPTURES_DIR / (name[:-3] + ".sigmf-meta") if name.endswith(".iq") else None
        item["sigmf"] = bool(meta and meta.exists())
    return items


def _emit_captures_list() -> None:
    socketio.emit("captures", {"items": _enriched_captures()})


def _emit_adsb(aircraft: list[dict], meta: dict) -> None:
    for a in aircraft:
        hx = a.get("hex")
        if not hx:
            continue
        reg = a.get("r") or icao_to_registration(hx)   # prefer readsb's own
        if reg:
            a["registration"] = reg
        country = icao_country(hx)
        if country:
            a["country"] = country
        label = (a.get("flight") or "").strip() or reg or hx.upper()
        info = {k: v for k, v in {"registration": reg, "country": country,
                                  "alt": a.get("alt_baro"), "gs": a.get("gs")}.items()
                if v is not None}
        _record_contact(f"adsb:{hx}", "adsb", label, ident=hx, info=info,
                        lat=a.get("lat"), lon=a.get("lon"))
    socketio.emit("adsb", {"aircraft": aircraft, "meta": meta})


def _emit_scan(name: str, payload: dict) -> None:
    socketio.emit(name, payload)


# ------------------------------------------------------------------
# Job lifecycle. One board, one publish path.
# ------------------------------------------------------------------

def _halt(worker) -> None:
    if hasattr(worker, "cancel"):
        worker.cancel()
    else:
        worker.stop()


def _stop_all_locked() -> None:
    """Clear the current job and tear its worker down. Caller holds _state_lock."""
    _finalize_audio_record_locked()
    job = _jobs.clear()
    _emit_status()
    if job is not None and job.kind == "scan":
        socketio.emit("scan_stopped", {})
    if job is None:
        return
    try:
        job.stop()
    except Exception:
        log.exception("teardown of %s failed", job.kind)


def _finish_job(identity: int) -> bool:
    """Drop the job when it is still the published one."""
    with _state_lock:
        job = _jobs.finish(identity)
        if job is not None and job.kind == "radio":
            _finalize_audio_record_locked()
    return job is not None


def _exit_handler(identity: int, kind: str):
    death_label = mode_spec(kind).death_label

    def on_exit(reason: str) -> None:
        if reason == "stopped":
            return
        if not _finish_job(identity):
            return
        telemetry.bump("subprocess_deaths")
        if _device["info"] is not None:
            _emit_toast("error", f"{death_label} stopped unexpectedly.")
        _emit_status()
    return on_exit


def _begin_job(kind: str, make):
    """Stop the previous job, start the worker, and publish only if start() returns.

    make(identity) builds the worker and does not start it. Returns
    (True, start_value) when the job is published. start_value may be None.
    Returns (False, None) when the device is missing or start fails. A failed
    start leaves the board idle.
    """
    spec = mode_spec(kind)
    if spec.uses_device and _device["info"] is None:
        _emit_toast("error", "Cannot start: HackRF not detected.")
        return False, None
    with _state_lock:
        _stop_all_locked()
        identity = _jobs.take_identity()
        try:
            worker = make(identity)
        except Exception:
            log.exception("start %s failed", kind)
            _emit_toast("error", f"Could not start {spec.death_label.lower()}")
            return False, None
        try:
            value = worker.start()
        except Exception:
            try:
                _halt(worker)
            except Exception:
                log.exception("cleanup after failed %s start", kind)
            log.exception("start %s failed", kind)
            _emit_toast("error", f"Could not start {spec.death_label.lower()}")
            return False, None
        _jobs.publish(kind, worker, lambda w=worker: _halt(w), identity)
    _emit_status()
    return True, value


def _start_sweep(cfg: SweepConfig) -> bool:
    def make(identity: int):
        _state["sweep_config"] = cfg
        return SweepStreamer(
            cfg,
            on_sweep=_emit_sweep,
            on_exit=_exit_handler(identity, "sweep"),
        )
    ok, _value = _begin_job("sweep", make)
    return ok


def _start_decode(cfg: DecodeConfig) -> bool:
    def make(identity: int):
        _state["decode_config"] = cfg
        return Rtl433Decoder(
            cfg,
            on_event=_emit_event,
            on_exit=_exit_handler(identity, "decode"),
        )
    ok, _value = _begin_job("decode", make)
    return ok


def _start_capture(cfg: CaptureConfig) -> bool:
    geo = _receiver_geotag()

    def on_done(identity: int):
        def _on_done(record, reason: str) -> None:
            cleared = _finish_job(identity)
            if reason == "completed":
                size_mb = record.file_size / (1024 * 1024)
                _emit_toast("info", f"Capture done: {record.name} ({size_mb:.1f} MB)")
            elif reason == "cancelled":
                _emit_toast("warn", f"Capture cancelled: {record.name}")
            elif _device["info"] is not None:
                _emit_toast("error", f"Capture failed: {record.name}")
            socketio.emit("capture_done", {"record": asdict(record), "reason": reason})
            _emit_captures_list()
            if cleared:
                _emit_status()
        return _on_done

    def make(identity: int):
        _state["capture_config"] = cfg
        return IqCapture(
            cfg,
            on_progress=_emit_capture_progress,
            on_done=on_done(identity),
            geolocation=geo,
        )

    ok, value = _begin_job("capture", make)
    if not ok:
        return False
    socketio.emit("capture_started", {"record": asdict(value)})
    return True


def _start_adsb(cfg: AdsbConfig) -> bool:
    def make(identity: int):
        _state["adsb_config"] = cfg
        return AdsbReceiver(
            cfg,
            on_update=_emit_adsb,
            on_exit=_exit_handler(identity, "adsb"),
        )
    ok, _value = _begin_job("adsb", make)
    return ok


def _start_radio(cfg: RadioConfig) -> bool:
    def make(identity: int):
        _state["radio_config"] = cfg
        return RadioReceiver(
            cfg,
            on_audio=_on_radio_audio,
            on_exit=_exit_handler(identity, "radio"),
            on_signal=lambda db: socketio.emit("radio_signal", {"dbfs": db}),
        )
    ok, _value = _begin_job("radio", make)
    if not ok:
        return False
    socketio.emit(
        "radio_started",
        {"sample_rate": AUDIO_RATE, "freq_mhz": cfg.freq_mhz, "demod": cfg.demod},
    )
    return True


def _start_scan_radio(cfg: ScanRadioConfig) -> bool:
    if not cfg.freqs_mhz:
        _emit_toast("error", "No marked frequencies to scan.")
        return False

    def make(identity: int):
        return RadioScanner(
            cfg,
            on_audio=lambda pcm: socketio.emit("radio_audio", pcm),
            on_event=lambda name, payload: socketio.emit(name, payload),
            on_exit=_exit_handler(identity, "scan_radio"),
        )
    ok, _value = _begin_job("scan_radio", make)
    if not ok:
        return False
    socketio.emit(
        "scan_radio_started",
        {"count": len(cfg.freqs_mhz), "sample_rate": AUDIO_RATE, "demod": cfg.demod},
    )
    return True


def _capture_match(name: str):
    return next((c for c in list_captures() if c.get("name") == name), None)


def _start_replay(name: str) -> bool:
    if capture_basename(name) is None:
        _emit_toast("error", "Invalid capture name")
        return False
    match = _capture_match(name)
    if not match or not match.get("path") or not os.path.exists(match["path"]):
        _emit_toast("error", "Capture not found")
        return False
    center = int(match.get("freq_hz") or 0)
    sr = int(match.get("sample_rate") or 2_000_000)

    def on_done(identity: int):
        def _on_done(reason: str) -> None:
            cleared = _finish_job(identity)
            socketio.emit("replay_done", {"reason": reason, "name": name})
            if cleared:
                _emit_status()
        return _on_done

    def make(identity: int):
        return IqReplay(
            match["path"], center, sr,
            on_frame=lambda f0, f1, powers: socketio.emit(
                "sweep", {"f0": f0, "f1": f1, "powers": powers, "rate_hz": 20.0}),
            on_done=on_done(identity),
        )
    ok, _value = _begin_job("replay", make)
    if not ok:
        return False
    socketio.emit("replay_started", {"name": name, "freq_hz": center, "sample_rate": sr})
    return True


def _start_iq_play(name: str, demod: str, offset_hz: float) -> bool:
    if capture_basename(name) is None or not name.endswith(".iq"):
        _emit_toast("error", "Invalid capture")
        return False
    match = _capture_match(name)
    if not match or not match.get("path") or not os.path.exists(match["path"]):
        _emit_toast("error", "Capture not found")
        return False
    if demod not in ("fm", "nfm", "am"):
        demod = "fm"
    sr = int(match.get("sample_rate") or 2_000_000)
    center = int(match.get("freq_hz") or 0)

    def on_done(identity: int):
        def _on_done(reason: str) -> None:
            cleared = _finish_job(identity)
            socketio.emit("iq_play_done", {"reason": reason, "name": name})
            if cleared:
                _emit_status()
        return _on_done

    def make(identity: int):
        return IqAudioPlayer(
            match["path"], sr, demod, offset_hz,
            on_audio=lambda pcm: socketio.emit("radio_audio", pcm),
            on_done=on_done(identity),
        )
    ok, _value = _begin_job("iq_play", make)
    if not ok:
        return False
    socketio.emit("radio_started", {"sample_rate": AUDIO_RATE, "freq_mhz": center / 1e6, "demod": demod})
    socketio.emit("iq_play_started", {"name": name, "demod": demod, "offset_hz": offset_hz,
                                      "center_hz": center, "sample_rate": sr})
    return True


def _start_decode_file(name: str) -> bool:
    if capture_basename(name) is None or not name.endswith(".iq"):
        _emit_toast("error", "Invalid capture")
        return False
    match = _capture_match(name)
    if not match or not match.get("path") or not os.path.exists(match["path"]):
        _emit_toast("error", "Capture not found")
        return False
    sr = int(match.get("sample_rate") or 2_000_000)
    center = int(match.get("freq_hz") or 0)

    def on_done(identity: int):
        def _on_done(reason: str) -> None:
            cleared = _finish_job(identity)
            socketio.emit("decode_file_done", {"reason": reason, "name": name})
            if reason == "completed":
                _emit_toast("info", f"Decode finished: {name}")
            if cleared:
                _emit_status()
        return _on_done

    def make(identity: int):
        return IqFileDecoder(
            match["path"], sr, center,
            on_event=_emit_event,
            on_done=on_done(identity),
        )
    ok, _value = _begin_job("decode_file", make)
    if not ok:
        return False
    socketio.emit("decode_file_started", {"name": name, "freq_hz": center, "sample_rate": sr})
    _emit_toast("info", f"Decoding {name} (this can take a moment)")
    return True


def _start_scan(cfg: ScanConfig) -> bool:
    def on_scan_event(identity: int):
        def _on_scan_event(name: str, payload: dict) -> None:
            if name in ("scan_completed", "scan_failed"):
                cleared = _finish_job(identity)
                _emit_scan(name, payload)
                if cleared:
                    _emit_status()
            else:
                _emit_scan(name, payload)
        return _on_scan_event

    def make(identity: int):
        _state["scan_config"] = cfg
        return AutoScanner(cfg, on_event=on_scan_event(identity))
    ok, _value = _begin_job("scan", make)
    return ok


def _stop_all_external() -> None:
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
            busy = _jobs.uses_device()
        if busy:
            # A device job owns the HackRF; skip the probe so hackrf_info doesn't
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
    try:
        _gps.stop()
    except Exception:
        log.exception("error stopping gps client")


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
        snapshot = _status_fields_locked()
    # Store I/O stays outside _state_lock (same rule as _emit_status): never
    # hold the state lock across SQLite calls. bookmarks/settings ride the
    # initial connect snapshot only; _emit_status() intentionally omits them.
    snapshot["bookmarks"] = get_store().list_bookmarks()
    snapshot["settings"] = get_store().all_settings()
    emit("status", snapshot)
    emit("device_status", {"info": _device["info"], "checked_at": _device["checked_at"]})
    emit("gps_status", _gps.position())
    emit("audio_record_status", _audio_status_snapshot())
    emit("contacts", {"data": get_store().list_contacts()})
    emit("captures", {"items": _enriched_captures()})


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


@socketio.on("start_radio")
def on_start_radio(data):
    try:
        cfg = RadioConfig(**_filter_payload(data, RadioConfig))
    except (TypeError, ValueError) as e:
        _emit_toast("error", f"Invalid radio config: {e}")
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
    if name and _start_replay(name):
        try:
            get_store().touch_capture(int(time.time()), name)
        except Exception:
            log.exception("touch_capture failed")


@socketio.on("play_capture")
def on_play_capture(data):
    data = data or {}
    name = data.get("name")
    if not isinstance(name, str) or not name:
        _emit_toast("error", "play_capture requires a name")
        return
    demod = data.get("demod", "fm")
    try:
        offset = float(data.get("offset_hz", 0) or 0)
    except (TypeError, ValueError):
        offset = 0.0
    if _start_iq_play(name, demod, offset):
        try:
            get_store().touch_capture(int(time.time()), name)
        except Exception:
            log.exception("touch_capture failed")


@socketio.on("decode_capture")
def on_decode_capture(data):
    name = (data or {}).get("name")
    if not isinstance(name, str) or not name:
        _emit_toast("error", "decode_capture requires a name")
        return
    _start_decode_file(name)


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
        capture = _jobs.worker("capture")
        if capture is not None:
            try:
                capture.cancel()
            except Exception:
                log.exception("cancel_capture failed")


@socketio.on("stop")
def on_stop():
    _stop_all_external()


@socketio.on("snap_radio")
def on_snap_radio():
    """Recenter the radio on the strongest carrier near the tuned frequency."""
    with _state_lock:
        recv = _jobs.worker("radio")
        cfg = _state["radio_config"]
    if recv is None:
        _emit_toast("error", "Snap only works while the radio is playing.")
        return
    iq = recv.latest_iq()
    if iq is None:
        _emit_toast("info", "No samples yet, try snapping again in a moment.")
        return
    # Snap fine-centers the station you are on, it does not seek nearby ones.
    # Keep the search well inside the channel spacing so a stronger neighbor (or
    # its modulation skirt) cannot win: FM channels are 200 kHz apart, narrow
    # AM/NFM channels much tighter.
    search_hz = 50_000.0 if cfg.demod == "fm" else 15_000.0
    offset = find_peak_offset(iq, cfg.sample_rate_hz, search_hz=search_hz)
    if abs(offset) < 500.0:
        _emit_toast("info", f"No stronger signal to snap to near {cfg.freq_mhz:.3f} MHz.")
        return
    new_freq = round(cfg.freq_mhz + offset / 1_000_000.0, 4)
    try:
        snapped = replace(cfg, freq_mhz=new_freq)
    except ValueError as exc:
        _emit_toast("error", str(exc))
        return
    _start_radio(snapped)
    _emit_toast("success", f"Snapped to {new_freq:.3f} MHz ({offset / 1000:+.1f} kHz).")


@socketio.on("refresh_device")
def on_refresh_device():
    with _state_lock:
        busy = _jobs.uses_device()
    # Don't probe a device that a running job owns (hackrf_info would fail and
    # falsely flip the pill to "disconnected"); just re-broadcast current status.
    if not busy:
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
        get_store().delete_capture_annotation(name)
        _emit_toast("info", f"Deleted {name}")
    _emit_captures_list()


@socketio.on("update_capture")
def on_update_capture(data):
    data = data or {}
    filename = data.get("filename")
    if not isinstance(filename, str) or not filename:
        _emit_toast("error", "update_capture requires a filename")
        return
    # Annotations are keyed by filename and may outlive the file (the captures
    # list flags missing .iq files); orphan rows are cleared on delete_capture.
    try:
        get_store().upsert_capture_annotation(
            filename,
            data.get("user_label", ""),
            data.get("notes", ""),
            data.get("tags", []),
        )
    except (ValueError, TypeError) as e:
        _emit_toast("error", str(e))
        return
    except Exception:
        log.exception("update_capture failed")
        _emit_toast("error", "Could not save capture annotation")
        return
    _emit_captures_list()


@socketio.on("list_bookmarks")
def on_list_bookmarks():
    _emit_bookmarks()


@socketio.on("add_bookmark")
def on_add_bookmark(data):
    data = data or {}
    try:
        freq_hz = int(data["freq_hz"])
        label = data["label"]
    except (KeyError, TypeError, ValueError):
        _emit_toast("error", "add_bookmark requires freq_hz (int) and label")
        return
    source = data.get("source", "user")
    if source not in _BOOKMARK_SOURCE_ALLOWLIST:
        source = "user"
    try:
        get_store().add_bookmark(
            int(time.time()),
            freq_hz,
            data.get("demod"),
            label,
            data.get("notes", ""),
            data.get("tags", []),
            source,
        )
    except ValueError as e:
        _emit_toast("error", str(e))
        return
    _emit_bookmarks()
    _emit_toast("success", f"Bookmark added: {label}")


@socketio.on("update_bookmark")
def on_update_bookmark(data):
    data = data or {}
    try:
        bm_id = int(data["id"])
    except (KeyError, TypeError, ValueError):
        _emit_toast("error", "update_bookmark requires id")
        return
    fields = {}
    for key in ("freq_hz", "demod", "label", "notes", "tags"):
        if key in data:
            fields[key] = data[key]
    if "freq_hz" in fields:
        try:
            fields["freq_hz"] = int(fields["freq_hz"])
        except (TypeError, ValueError):
            _emit_toast("error", "freq_hz must be an integer")
            return
    try:
        result = get_store().update_bookmark(int(time.time()), bm_id, **fields)
    except ValueError as e:
        _emit_toast("error", str(e))
        return
    if result is None:
        _emit_toast("error", f"Bookmark {bm_id} not found")
        return
    _emit_bookmarks()


@socketio.on("delete_bookmark")
def on_delete_bookmark(data):
    data = data or {}
    try:
        bm_id = int(data["id"])
    except (KeyError, TypeError, ValueError):
        _emit_toast("error", "delete_bookmark requires id")
        return
    get_store().delete_bookmark(bm_id)
    _emit_bookmarks()


@socketio.on("bump_bookmark")
def on_bump_bookmark(data):
    data = data or {}
    try:
        bm_id = int(data["id"])
    except (KeyError, TypeError, ValueError):
        _emit_toast("error", "bump_bookmark requires id")
        return
    get_store().bump_bookmark(int(time.time()), bm_id)
    _emit_bookmarks()


@socketio.on("set_setting")
def on_set_setting(data):
    data = data or {}
    try:
        key = data["key"]
        value = data["value"]
    except KeyError:
        _emit_toast("error", "set_setting requires key and value")
        return
    if key not in _SETTING_ALLOWLIST:
        return  # silently ignore non-allowlisted keys
    try:
        get_store().set_setting(key, value)
    except Exception as e:
        _emit_toast("error", str(e))
        return
    if key == "gps_enabled":
        want = bool(value) and not _GPS_FORCED_OFF
        _gps.set_enabled(want)
        if bool(value) and _GPS_FORCED_OFF:
            _emit_toast("warn", "GPS is disabled on this host (AETHERSCOPE_GPS=0)")
        _emit_gps_status()


@socketio.on("redact_capture")
def on_redact_capture(data):
    name = (data or {}).get("name")
    if not isinstance(name, str) or not name:
        _emit_toast("error", "redact_capture requires a name")
        return
    if redact_location(name):
        _emit_toast("info", f"Location removed from {name}")
    _emit_captures_list()


@socketio.on("start_audio_record")
def on_start_audio_record(data):
    label = (data or {}).get("label", "")
    label = label[:80] if isinstance(label, str) else ""
    with _state_lock:
        running = _jobs.worker("radio") is not None
        already = _audio_rec["recorder"] is not None
        cfg = _state["radio_config"]
    if not running:
        _emit_toast("error", "Start the radio first, then record.")
        return
    if already:
        _emit_audio_record_status()
        return
    # Geotag like an IQ capture (opt-in GPS, coarsened); None if no fix.
    geo = _gps.geolocation()
    if geo is not None:
        try:
            geo = coarsen_geolocation(geo, str(get_store().get_setting("gps_precision", "full")))
        except Exception:
            log.exception("coarsen geolocation failed")
    try:
        rec, record = start_recording(
            CAPTURES_DIR, int(time.time()), int(round(cfg.freq_mhz * 1_000_000)),
            cfg.demod, AUDIO_RATE, label, geo,
        )
    except Exception:
        log.exception("start_audio_record failed")
        _emit_toast("error", "Could not start recording.")
        return
    with _state_lock:
        if _audio_rec["recorder"] is not None:
            # A concurrent start_audio_record won the race; discard ours so we
            # don't orphan a half-written WAV.
            try:
                rec.close()
                delete_capture(record.name)
            except Exception:
                log.exception("cleanup of raced recording failed")
            _emit_audio_record_status()
            return
        _audio_rec["recorder"] = rec
        _audio_rec["record"] = record
    _emit_audio_record_status()
    _emit_toast("info", f"Recording audio to {record.name}")


@socketio.on("stop_audio_record")
def on_stop_audio_record(data=None):
    with _state_lock:
        record = _audio_rec["record"]
        active = _audio_rec["recorder"] is not None
        _finalize_audio_record_locked()
    if active and record is not None:
        _emit_toast("info", f"Saved {record.name} ({record.duration_s:.1f}s)")


@socketio.on("list_inventory")
def on_list_inventory():
    _emit_contacts()


@socketio.on("clear_inventory")
def on_clear_inventory(data=None):
    try:
        n = get_store().clear_contacts()
    except Exception as e:
        _emit_toast("error", str(e))
        return
    _contact_write_ts.clear()
    _emit_contacts()
    _emit_toast("info", f"Cleared {n} contacts")


def resolve_port(raw, default: int = 8765) -> int:
    """Parse AETHERSCOPE_PORT, falling back to default on missing/invalid/out-of-range."""
    try:
        p = int(raw)
    except (TypeError, ValueError):
        return default
    return p if 1 <= p <= 65535 else default


def main() -> None:
    # Bind localhost by default; containers set AETHERSCOPE_HOST=0.0.0.0 and
    # rely on `-p 127.0.0.1:8765:8765` (and a reverse proxy) for exposure.
    # The port is configurable via AETHERSCOPE_PORT (default 8765).
    host = os.environ.get("AETHERSCOPE_HOST", "127.0.0.1")
    port = resolve_port(os.environ.get("AETHERSCOPE_PORT"))
    log.info("Aetherscope listening on http://%s:%d", host, port)
    try:
        seeded = get_store().seed_presets_once(int(time.time()), PRESETS)
        if seeded:
            log.info("Seeded %d preset bookmarks", seeded)
    except Exception:
        log.exception("Failed to seed presets; continuing without them")
    # Register cleanup for graceful shutdown so we don't orphan subprocesses
    # holding the HackRF when launchd sends SIGTERM.
    atexit.register(_shutdown)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, lambda *_: (_shutdown(), sys.exit(0)))
        except (OSError, ValueError):
            pass  # not running on main thread; atexit still covers most cases
    threading.Thread(target=_device_poller, daemon=True).start()
    # GPS geotagging: honor the stored opt-in toggle (unless hard-disabled by
    # env). The client thread idles cheaply while disabled.
    try:
        if not _GPS_FORCED_OFF and bool(get_store().get_setting("gps_enabled", False)):
            _gps.set_enabled(True)
    except Exception:
        log.exception("Failed to read gps_enabled; leaving GPS off")
    _gps.start()
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
