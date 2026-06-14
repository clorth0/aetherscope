# Architecture

```
  hackrf_sweep  ┐
  hackrf_transfer├─ stdout / JSON / files ─▶  Flask + Socket.IO  ─ Socket.IO ─▶  Browser
  rtl_433       │                             (one-job mutex,                    canvas + map
  readsb-hackrf ┘                              orchestration)                    + audio + log
```

- **One device, one job.** Every HackRF-claiming job (sweep, decode, capture,
  ADS-B, radio, scanner, auto-scan) is mutually exclusive through a single
  "current job" slot guarded by a reentrant lock. IQ replay is the exception: it
  reads a file and uses no device.
- **Device poller** probes `hackrf_info` every 2.5 s to show live device status,
  and pauses while a job owns the HackRF so it does not contend with the running
  subprocess.
- **DSP in numpy/scipy.** FM/NFM/AM demodulation, channel-power (`signal_dbfs`),
  and replay FFT frames are computed in the Python process. The sweep assembler
  rebuilds one coherent spectrum row per `hackrf_sweep` cycle.
- **Telemetry.** Subprocess stderr (drops/overruns) plus server counters
  (sweeps computed vs emitted, subprocess deaths) feed the Diagnostics panel.

## Tech stack

Flask + Flask-SocketIO (threading async), numpy + scipy, a vanilla-JS canvas
front end with the Web Audio API (AudioWorklet) and Leaflet. Managed with `uv`.
Front-end dependencies are vendored locally and served under a strict
Content-Security-Policy, so the app shell works fully offline.

## Layout

- `backend/`: Flask app (`app.py`), per-mode subprocess wrappers
  (`sdr.py`, `radio.py`, `decoders.py`, `adsb.py`, `capture.py`, `scan.py`),
  offline `replay.py`, `telemetry.py`, `device.py`.
- `frontend/`: `templates/index.html`, `static/` (canvas/UI JS, AudioWorklet,
  CSS, vendored deps).
- `deploy/`: installers, launchd template, `restart.sh`, `Caddyfile.example`.
- `tests/`: synthetic, hardware-free unit tests (run with `uv run python tests/test_*.py`).
