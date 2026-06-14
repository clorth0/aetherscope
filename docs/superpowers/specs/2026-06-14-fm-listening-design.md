# Aetherscope FM Listening (v1) Design

Date: 2026-06-14
Status: Approved

## Overview

Add an "FM" mode to Aetherscope that lets the user tune anywhere in the
88 to 108 MHz broadcast band and hear mono audio in the browser, with
station presets, play/stop, and a volume control. FM listening claims the
HackRF exclusively, the same one-job-at-a-time model used by Sweep, Decode,
Capture, ADS-B, and Auto-Scan.

## Goals

- Tune to any frequency in 88 to 108 MHz and hear it.
- Station presets, play/stop, volume.
- Self-contained: no new system tools beyond what `deploy/install.sh`
  already installs (`hackrf_transfer`). numpy is already a dependency.
- Mirror the existing receiver patterns so the code is easy to follow.

## Non-goals (v1, deliberately deferred)

- Stereo FM (pilot tone / MPX decode).
- RDS station-name text.
- Recording the audio.
- Narrowband FM / airband / other bands.
- Live spectrum display while listening.

## Architecture

Three layers, matching the existing app structure.

### 1. DSP and device: `backend/fm.py` (new)

- `FmConfig` dataclass:
  - `freq_mhz: float` (default about 101.1)
  - `sample_rate_hz: int = 2_000_000` (HackRF minimum sample rate)
  - `lna_gain: int`, `vga_gain: int`, `amp_enable: bool` (RX gain, same
    semantics as the sweep gains)
- `FmReceiver` class, same shape as `AdsbReceiver` / `SweepStreamer`:
  - `start()`, `stop()`, `is_running()`, `_run()`.
  - `_run()` spawns `hackrf_transfer` streaming signed 8-bit interleaved
    I/Q (cs8, the format `capture.py` already records) to stdout, reads it
    in fixed-size blocks (about 0.1 s of samples), and runs a wideband-FM
    demodulation chain.
  - Calls an `on_audio(pcm_bytes: bytes)` callback per processed block, and
    an `on_exit(reason)` callback on teardown / unexpected death (same
    "stopped" vs "died" contract as the other receivers).
  - Binary path resolution uses the existing robust pattern:
    `shutil.which("hackrf_transfer") or "/opt/homebrew/bin/hackrf_transfer"`.

- Demodulation chain (wideband FM, mono), implemented as a pure, testable
  function `fm_demodulate(iq: np.ndarray, state) -> np.ndarray`:
  1. cs8 bytes to complex64 (I + jQ, scaled to about [-1, 1]).
  2. Low-pass and decimate from 2,000,000 to about 250,000 (the FM IF).
  3. FM discriminator: `angle(x[n] * conj(x[n-1]))`.
  4. 75 microsecond de-emphasis (single-pole IIR), US broadcast standard.
  5. Low-pass and decimate from about 250,000 to 48,000 mono.
  6. Convert to int16 PCM.
  - Decimation factors and filter taps are an implementation detail; the
    target output is mono int16 at a fixed audio rate (about 48 kHz). Filter
    and de-emphasis state carry across blocks so there are no seams.

### 2. Wiring: `backend/app.py`

- Add a `"fm"` mode:
  - `_state` gains an `fm` slot and an `fm_config` slot; `FmConfig` default
    added to the initial state.
  - `_start_fm(cfg)` follows `_start_adsb`: refuse if no device, stop all,
    bump generation, create `FmReceiver`, store it, set mode, start.
  - `on_audio` forwards PCM as a binary Socket.IO event:
    `socketio.emit("fm_audio", pcm_bytes)`. An initial `fm_started` event
    carries `{ "sample_rate": <audio_hz>, "freq_mhz": <f> }`.
  - `start_fm` socket handler (validate via `_filter_payload(data, FmConfig)`).
  - Reuse the existing `stop` handler.
  - Add `fm` to `_stop_all_locked`'s slot list, to the `connect` snapshot,
    and to `_emit_status`.

### 3. Frontend: `index.html`, `waterfall.js`, `style.css`, new AudioWorklet

- New tab: `<button class="mode-tab" data-mode="fm">FM</button>`.
- New control pane `pane-fm`:
  - Frequency input (number, step 0.1 MHz) plus minus/plus step buttons.
  - Station preset buttons.
  - Large Play / Stop control.
  - Volume slider.
  - Status line ("Playing 101.1 MHz" / "Stopped").
- New display view `view-fm`: minimal status (tuned frequency, playing
  indicator).
- Audio playback:
  - On Play (a user gesture, so browser autoplay policy is satisfied):
    create an `AudioContext`, add an `AudioWorklet` module that holds a ring
    buffer, then emit `start_fm` with the tuned frequency.
  - Incoming `fm_audio` binary frames: int16 to float32, pushed into the
    worklet ring buffer for glitch-free playback. Volume via a `GainNode`.
  - On Stop: emit `stop`, then tear down the audio graph.
  - The AudioWorklet processor lives in a new static file (for example
    `frontend/static/fm-audio-worklet.js`), loaded with `addModule`.

## Data flow

```
HackRF --(cs8 IQ)--> hackrf_transfer --stdout--> FmReceiver._run
  -> fm_demodulate (numpy: decimate, discriminate, de-emphasis, decimate)
  -> int16 PCM block -> on_audio
  -> socketio.emit("fm_audio", pcm) -> browser
  -> int16 to float32 -> AudioWorklet ring buffer -> GainNode -> speakers
```

## Error handling

- `hackrf_transfer` missing: toast error, do not start (same as other modes).
- Device busy / not detected: `_start_fm` refuses with a toast, like
  `_start_adsb`.
- Subprocess dies unexpectedly: `on_exit("died")` -> generation-checked
  state reset + error toast (existing `_make_exit_handler` style).
- Browser `AudioContext` suspended: resumed on the Play click.

## Testing

- Unit test (`tests/test_fm.py`, no hardware): synthesize IQ for a known
  audio tone frequency-modulated onto a carrier, run `fm_demodulate`, and
  assert the recovered audio's dominant frequency matches the input tone
  (within tolerance). Covers the core DSP correctness.
- On-device verification: tune to a known local station and confirm audible,
  intelligible audio; confirm Stop frees the device and a subsequent Sweep
  works (mutex behaves).

## File-by-file change list

- `backend/fm.py` (new): `FmConfig`, `fm_demodulate`, `FmReceiver`.
- `backend/app.py`: state slots, `_start_fm`, `start_fm` handler, `fm` in
  `_stop_all_locked` / connect snapshot / `_emit_status`, `fm_audio` and
  `fm_started` emits.
- `frontend/templates/index.html`: FM tab, `pane-fm`, `view-fm`.
- `frontend/static/waterfall.js`: FM controls, Web Audio playback wiring.
- `frontend/static/fm-audio-worklet.js` (new): ring-buffer AudioWorklet.
- `frontend/static/style.css`: styles for the FM pane controls as needed.
- `tests/test_fm.py` (new): demodulator unit test.
- `README.md`: add FM to the modes table.
```
