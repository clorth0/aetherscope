# Aetherscope Instrument + Observability Upgrades — Implementation Plan

> **For agentic workers:** execute task-by-task, TDD where logic exists, commit per task. Steps use `- [ ]`.

**Goal:** Make Aetherscope a stronger measurement instrument (SA-grade spectrum features, NBFM, calibration) and a better-instrumented service (drop-sample telemetry, diagnostics, CI), while hardening it for offline/self-hosted use.

**Architecture:** Mostly additive. Frontend SA features are client-side in `waterfall.js` (live FFT already provides the data). New demod is a small extension to the existing `radio.py` chain. Observability surfaces data already produced (subprocess stderr, throttle counters). Robustness work removes the CDN dependency and tightens CORS/CSP.

**Tech stack:** Flask + Socket.IO (threading), numpy/scipy, vanilla JS canvas, uv, Playwright (verify), GitHub Actions (CI).

**Execution:** Inline + sequential (shared files), TDD for backend logic, Playwright screenshots to verify UI. Commit + push + `deploy/restart.sh` per task. Keep the launchd service live.

---

## Phase 1 — Spectrum-analyzer instrument features (frontend)

### Task 1.1: Max-hold + average traces
- Files: `frontend/static/waterfall.js`, `index.html` (sweep view toggle), `style.css`.
- Approach: keep `maxPowers` / running-average Float32Arrays sized to the current sweep; element-wise update per `sweep` event; draw dim overlay traces in `drawFFT`. Toggle buttons (Max Hold, Avg, Clear) in the sweep view; reset on range change.
- Verify: Playwright screenshot showing live + max-hold traces during a sweep.
- Commit.

### Task 1.2: Live peak table + peak-search + delta markers
- Files: `frontend/static/waterfall.js`, `index.html` (peak table panel in sweep view), `style.css`.
- Approach: port Auto-Scan's peak logic (`_extract_peaks`: median floor + threshold + grouping) to a JS `findPeaks(powers, f0, f1)`; render a sortable table (freq, dBFS, SNR, band via a JS mirror of the band labels) updated ~2 Hz. "Mark strongest" button adds the top peak. Delta readout when 2 marks exist (Δf, ΔdB).
- Verify: screenshot of peak table during FM sweep showing 107.6 on top.
- Commit.

### Task 1.3: Click-drag zoom on the spectrum
- Files: `frontend/static/waterfall.js`.
- Approach: mousedown+drag on FFT selects an x-range → translate to freq range → set `#f_start`/`#f_stop` and restart sweep. Double-click resets to last full range.
- Verify: screenshot before/after zoom.
- Commit.

## Phase 2 — Radio: NBFM + scanner

### Task 2.1: NBFM demod
- Files: `backend/radio.py`, `tests/test_radio.py`, `index.html` (FM/AM/NFM toggle), `frontend/static/waterfall.js`.
- TDD: `test_nfm_recovers_tone` — synth NBFM (deviation ~3 kHz) → `demod_nfm` recovers tone. RED → implement (narrow ~12.5 kHz channel filter, discriminator, no/҆light de-emphasis, audio LP) → GREEN.
- UI: add "NFM" to the demod segmented toggle; validation in `app.py` `start_radio` accepts `nfm`.
- Verify: unit test + on-device listen at a UHF/GMRS frequency.
- Commit.

### Task 2.2: Scanner mode
- Files: `backend/radio.py` (or new `backend/scanner.py`), `backend/app.py`, `index.html`, `frontend/static/waterfall.js`, `tests/`.
- Approach: cycle a frequency list (saved marks or a range step), dwell, measure `signal_dbfs`, stop on signal above a squelch threshold and stream audio; resume on user command or after timeout. Reuses one-job mutex.
- Verify: unit test for the step/squelch decision; on-device.
- Commit.

## Phase 3 — Calibration

### Task 3.1: ppm frequency correction + dB offset (display layer)
- Files: `frontend/static/waterfall.js`, `index.html` (settings inputs), persisted in localStorage.
- Approach: apply `freq * (1 + ppm/1e6)` to displayed frequencies and `+offset` to displayed power; axis label switches to dBm when offset set. Pure display, no backend change.
- Verify: screenshot with offset applied.
- Commit.

## Phase 4 — Observability / instrumentation

### Task 4.1: Capture subprocess stderr (dropped samples / overruns)
- Files: `backend/sdr.py`, `backend/radio.py`, `backend/capture.py`, `backend/adsb.py`, `backend/app.py`.
- Approach: stop `stderr=DEVNULL`; pipe stderr, read in the worker thread, parse hackrf/rtl lines for drop/overrun warnings, emit a throttled `telemetry` socket event + count. Keep noise low (only surface warnings).
- Verify: induce/observe a drop line in the diagnostics panel.
- Commit.

### Task 4.2: Diagnostics panel + counters
- Files: `backend/app.py` (counters: sweep computed vs emitted, restarts, device-poll ok/fail), `frontend/static/waterfall.js`, `index.html`, `style.css`.
- Approach: server tracks counters; emit `telemetry` ~1 Hz; a Diagnostics panel (collapsible) shows sweep rate, emit/drop, audio underruns (worklet reports buffer level back), device-poll success, subprocess restarts.
- Verify: panel populates during a sweep + radio.
- Commit.

### Task 4.3: Event/error log panel
- Files: `frontend/static/waterfall.js`, `index.html`, `style.css`.
- Approach: ring-buffer (last ~200) of toasts/telemetry warnings shown in a scrollable panel; existing `toast` events also pushed here.
- Commit.

## Phase 5 — Robustness / security

### Task 5.1: Vendor front-end deps + CSP
- Files: add `frontend/static/vendor/socket.io.min.js`, `leaflet.js`, `leaflet.css` (+ marker assets); `index.html` (point to local); `backend/app.py` (CSP header in `after_request`); remove SRI/CDN.
- Approach: download pinned versions into vendor/, serve locally, set a strict `Content-Security-Policy` (self for script/style, allow the ADS-B tile host + ws: connect). Works offline now.
- Verify: load with network blocked to confirm functionality; check CSP header present.
- Commit.

### Task 5.2: Pause device poller during active jobs
- Files: `backend/app.py`.
- Approach: in `_device_poller`, skip the `hackrf_info` probe while `_state["mode"] != "idle"` (assume present); resume when idle. Removes job-vs-poll contention.
- Commit.

### Task 5.3: Capture duration/sample-rate bounds
- Files: `backend/app.py` `on_start_capture` (validate duration <= cap, rate in HackRF range), `tests/`.
- TDD: invalid configs rejected with a toast.
- Commit.

## Phase 6 — Capability / ops

### Task 6.1: IQ replay (offline analysis of a saved capture)
- Files: `backend/replay.py` (read cs8, FFT frames), `backend/app.py` (replay mode + socket), `frontend` (replay controls reuse the sweep view), `tests/`.
- TDD: FFT-frame generator over a synth cs8 buffer.
- Commit.

### Task 6.2: POCSAG / paging decode (multimon-ng)
- Files: `deploy/install.sh` (add multimon-ng), `backend/paging.py`, `app.py`, frontend event list reuse, `tests/` (parse a sample line).
- Commit.

### Task 6.3: Linux Dockerfile + compose
- Files: `Dockerfile`, `compose.yml`, README.
- Approach: Linux image with the SDR stack; USB passthrough via `--device`/privileged; documented.
- Commit.

### Task 6.4: CI (GitHub Actions)
- Files: `.github/workflows/ci.yml`.
- Approach: on push/PR — `uv sync`, run `tests/test_*.py` (all synthetic, no hardware), `node --check` the JS. Badge in README.
- Commit.

---

## Self-review notes
- Coverage: every item from the ultrathink maps to a task (max-hold 1.1; peak table/markers 1.2; zoom 1.3; NBFM 2.1; scanner 2.2; ppm/dB cal 3.1; channel-power folded into 1.2 marker math; stderr telemetry 4.1; diagnostics 4.2; event log 4.3; vendor+CSP 5.1; poller 5.2; capture bounds 5.3; IQ replay 6.1; paging 6.2; Docker 6.3; CI 6.4).
- Ordering: instrument value first (Phase 1-2), then calibration, then observability, then robustness, then ops. Each task is independently shippable.
- Execution: TDD for all backend logic (radio, bands-style, capture bounds, replay, paging); UI verified via Playwright screenshots; commit + push + restart per task.
