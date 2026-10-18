# Architecture

```
  hackrf_sweep  ┐
  hackrf_transfer├─ stdout / JSON / files ─▶  Flask + Socket.IO  ─ Socket.IO ─▶  Browser
  rtl_433       │                             (one-job mutex,                    canvas + map
  readsb-hackrf ┘                              orchestration)                    + audio + log
```

- **One device, one job.** Every HackRF-claiming job (sweep, decode, capture,
  ADS-B, radio, scanner, auto-scan) is mutually exclusive through one current
  job on `jobs.JobBoard`, guarded by a reentrant lock. The job carries its
  status label and whether it uses the device. IQ replay, IQ listen, and file
  decode read a file and use no device, so a device probe can still run.
- **Device poller** probes `hackrf_info` every 2.5 s to show live device status,
  and pauses while a job owns the HackRF so it does not contend with the running
  subprocess.
- **DSP in numpy/scipy.** FM/NFM/AM demodulation, channel-power (`signal_dbfs`),
  snap-to-peak tuning (`tuning.py`), and replay FFT frames are computed in the
  Python process. The sweep assembler rebuilds one coherent spectrum row per
  `hackrf_sweep` cycle.
- **Telemetry.** Subprocess stderr (drops/overruns) plus server counters
  (sweeps computed vs emitted, subprocess deaths) feed the Diagnostics panel.
- **Auth boundary.** `auth.py` is the deep module behind who may drive
  the SDR: one `AuthPolicy` (bind host + optional `AETHERSCOPE_TOKEN`)
  decides whether auth is required, refuses a non-loopback bind without
  a token at startup, checks tokens in constant time, and issues the
  HttpOnly signed cookie the browser exchanges its token for. Loopback
  with no token keeps the historical no-auth behaviour. Enforcement has
  two seams over that one interface: a Flask `before_request` gate for
  HTTP (capture routes included) and the Socket.IO `connect` handler,
  which every socket handler sits behind.
- **Data layer.** `store.py` is a small stdlib-`sqlite3` module (single file at
  `AETHERSCOPE_DATA_DIR`, default `~/.local/share/aetherscope/`) holding
  bookmarks, persisted UI settings, capture annotations, and a persistent
  contact inventory (ADS-B aircraft + rtl_433 ISM devices, with first/last seen,
  count, info, location). It is thread-safe (one connection, WAL, a lock),
  parameterized throughout, and `user_version`-migrated.
- **GPS geotagging (optional, opt-in).** `gps.py` reads a local gpsd over a raw
  socket (`AETHERSCOPE_GPSD_HOST`/`PORT`, default 127.0.0.1:2947), only while the
  `gps_enabled` toggle is on (`AETHERSCOPE_GPS=0` hard-disables it). When a fresh
  fix exists, captures are stamped with a full-precision `geolocation` in their
  sidecar; a per-capture redaction action scrubs it. Position is pushed live as
  `gps_status`; lat/lon never reach the logs. Capture timestamps stay on the
  system clock (the puck has no PPS).
- **SigMF export.** Alongside each capture's `.iq` + UI sidecar, `sigmf.py`
  writes a companion `<base>.sigmf-meta` (SigMF 1.0.0) referencing the `.iq` via
  `core:dataset`, so recordings are portable to other SDR tools. Any geotag rides
  in `core:geolocation` (GeoJSON Point) and is scrubbed by redaction.
- **Contact sightings.** `contacts.py` is the deep module behind a
  contact sighting: it derives a `ContactSighting` (key, kind, label,
  ident, info, position) from a decoded rtl_433 event or an ADS-B
  aircraft dict, and owns dedup. Two keys are named explicitly, because
  they are genuinely different: the durable `inventory_key`
  (`ism:{model}:{ident}`, `adsb:{hex}` -- persisted as the contacts
  primary key, byte-identical so stored history is not orphaned) and
  the ephemeral `scan_key` (`{model}/{id|hex|channel}`, or hex alone
  for ADS-B -- one scan phase only, never persisted). Dedup likewise:
  a 2 s per-key `SightingThrottle` (anchored at the last passed
  sighting) gates live inventory writes, while `ScanSightings` dedupes
  for a whole scan phase with no TTL (first rtl_433 event wins and
  counts repeats; latest ADS-B aircraft per hex wins). Destination is
  not this module's decision: live sightings still go to the persistent
  inventory and Auto-Scan sightings still stay scan-only -- whether
  scan sightings should enter the inventory remains a parked question
  this module does not answer. `store.py` keeps only persistence.
- **Capture catalogue.** `catalogue.py` is the deep module behind the capture
  list: typed `IqCaptureEntry` / `AudioCaptureEntry` records (two shapes —
  IQ has sample rate/format and a SigMF companion, audio has demod/audio
  rate and none), one SigMF-name derivation, one slug, and sidecar
  listing/lookup/delete/redact. Callers get a capture ready to replay,
  play, or decode; the raw sidecar dict crosses no seam. Its `to_dict()`
  reproduces the sidecar exactly; the only server-added field in the
  enriched list is the derived `sigmf_name`.
- **Mode-config knowledge.** Field lists have exactly one authoritative
  home per side and no code generation joins them: Python dataclasses
  (`SweepConfig`, `DecodeConfig`, `CaptureConfig`, `AdsbConfig`,
  `RadioConfig`, `ScanRadioConfig`, `ScanConfig`, bounds in their
  constructors) define the server fields, and the hand-written
  `read*`/`apply*ConfigToInputs` mirrors in the per-mode `aether-*.js`
  modules define the browser fields — deliberately no build step, no
  deriving JS from Python, in keeping with the vendored/offline design.
  The one home for the *correspondence* between them is the static
  guard `tests/test_mode_config_guard.py`: it parses both sides and
  fails on drift, with intentional exceptions named there (decode
  `label` and radio gains/sample-rate are Python-only with no UI
  inputs; scan-radio's wire `{freqs, demod, squelch_dbfs}` is a named
  rename to `freqs_mhz`, not a dataclass mirror). The demod vocabulary
  itself has one Python home, `config.py` (`DEMODS`, plus the
  bookmark-only subset `BOOKMARK_DEMODS` that also allows `None`),
  consumed by `radio.py`, `app.py`, and `store.py`; the same guard
  checks the HTML demod controls against it.
- **Closing the loop.** Live radio audio can be recorded to WAV
  (`audio_record.py`). A saved `.iq` can be listened to (`iq_playback.py`: mix an
  offset within the band to baseband, resample to the demod rate, reuse
  `radio.demodulate`, stream real-time audio) or decoded (`decode_file.py`:
  resample to 1 MSps, convert cs8 to cu8, run `rtl_433 -r`). Both reuse existing
  paths (AudioWorklet, the Decode panel) and are device-free exclusive jobs.

## Tech stack

Flask + Flask-SocketIO (threading async), numpy + scipy, a vanilla-JS canvas
front end with the Web Audio API (AudioWorklet) and Leaflet. Managed with `uv`.
Front-end dependencies are vendored locally and served under a strict
Content-Security-Policy, so the app shell works fully offline.

## Layout

- `backend/`: Flask app (`app.py`), the mode-config vocabulary
  the auth-boundary module (`auth.py`), the mode-config vocabulary
  (`config.py`: the demod set and its bookmark subset — field lists
  themselves stay with the per-mode dataclasses), the single job board (`jobs.py`), the one
  worker lifecycle (`worker.py`: start/stop/is_running, terminate->wait->kill,
  and completed-vs-died outcome mapping behind one interface), the mode-launch
  module (`launch.py`: one declarative `LaunchSpec` per mode consumed by a
  single `Launcher`, owning the started/finish toast/status derivation in one
  place — JobBoard still owns job state and WorkerLifecycle still owns each
  worker's lifecycle), per-mode subprocess wrappers
  (`sdr.py`, `radio.py`, `decoders.py`, `adsb.py`, `capture.py`, `scan.py`),
  the capture catalogue (`catalogue.py`), the contact-sighting
  module (`contacts.py`),
  offline `replay.py`, snap-to-peak `tuning.py`, SQLite `store.py`,
  optional gpsd geotagging `gps.py`, SigMF export `sigmf.py`,
  WAV audio recording `audio_record.py`, IQ-capture playback `iq_playback.py`
  and file decode `decode_file.py`, ADS-B enrichment `adsb_enrich.py`,
  `telemetry.py`, `device.py`.
- `frontend/`: `templates/index.html`, `static/` (canvas/UI JS, AudioWorklet,
  CSS, vendored deps). The browser JS is plain classic scripts with no
  build step: `aether-core.js` (socket, shared state, status/toast
  plumbing behind the `window.Aether` seam) loads first, one
  `aether-<mode>.js` module per mode (sweep, decode, capture, adsb,
  scan, radio, inventory) registers its socket handlers and config
  hooks with the core, and `aether-boot.js` loads last.
- `deploy/`: installers, launchd template, `restart.sh`, `Caddyfile.example`.
- `tests/`: synthetic, hardware-free unit tests (run with `uv run python tests/test_*.py`).
