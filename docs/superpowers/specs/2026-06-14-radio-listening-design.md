# Aetherscope Radio Listening (AM/FM, v1) Design

Date: 2026-06-14
Status: Approved

## Overview

Add a "Radio" mode to Aetherscope that demodulates a tuned frequency to
mono audio in the browser, with a selectable AM/FM demodulator, station
presets, play/stop, and a volume control. Radio listening claims the
HackRF exclusively, the same one-job-at-a-time model used by the other
modes.

## Goals

- Tune a frequency and hear it, with the demodulator selectable:
  - FM (wideband): broadcast 88 to 108 MHz.
  - AM: airband (118 to 137 MHz) and other AM signals above 1 MHz.
- Station/frequency presets, play/stop, volume.
- Self-contained: no new system tools beyond `hackrf_transfer`. numpy and
  scipy are Python dependencies (scipy added for filtering/resampling).
- Mirror the existing receiver patterns.

## Hardware note

The HackRF One tunes 1 MHz to 6 GHz. The mediumwave AM broadcast band
(530 to 1700 kHz) is at or below the 1 MHz floor, so it is not reliably
receivable without an upconverter. The AM demodulator is therefore aimed
at AM signals above 1 MHz (airband especially), not mediumwave broadcast.

## Non-goals (v1, deliberately deferred)

- Stereo FM (pilot tone / MPX decode) and RDS station names.
- Narrowband FM (NFM) as a separate mode (FM here is wideband).
- Recording the audio.
- Live spectrum display while listening.
- Mediumwave AM broadcast (hardware-limited, see above).

## Architecture

Three layers, matching the existing app structure.

### 1. DSP and device: `backend/radio.py` (new)

- `RadioConfig` dataclass:
  - `demod: str = "fm"` ("fm" wideband or "am")
  - `freq_mhz: float` (default about 101.1)
  - `sample_rate_hz: int = 2_000_000` (HackRF minimum sample rate)
  - `lna_gain: int`, `vga_gain: int`, `amp_enable: bool`
- `RadioReceiver` class, same shape as `AdsbReceiver` / `SweepStreamer`
  (start/stop/is_running/_run). `_run()` spawns `hackrf_transfer` streaming
  signed 8-bit interleaved I/Q (cs8) to stdout, reads it in fixed blocks
  (about 0.1 s), demodulates, and calls `on_audio(pcm_bytes)` per block plus
  `on_exit(reason)` on teardown / death. Binary path resolution uses the
  existing `shutil.which(...) or "/opt/homebrew/bin/hackrf_transfer"` pattern.
- Pure, testable demodulators (filter/IIR state carried across blocks so
  there are no seams between blocks). Output is mono int16 at a fixed audio
  rate of 48000 (so it matches the browser AudioContext rate, no client
  resample):
  - `demod_fm(iq, state)`:
    1. cs8 to complex64 (scaled to about [-1, 1]).
    2. Low-pass and decimate 2,000,000 to about 250,000 (FM IF).
    3. Discriminator: `angle(x[n] * conj(x[n-1]))`.
    4. 75 microsecond de-emphasis (single-pole IIR).
    5. resample_poly to 48,000 mono.
  - `demod_am(iq, state)`:
    1. cs8 to complex64.
    2. Low-pass and decimate to 48,000 with a narrow channel filter (about
       8 kHz) suited to AM voice.
    3. Envelope detect: `abs(x)`.
    4. DC block (subtract slow mean / high-pass) so the carrier offset is
       removed.
  - A dispatcher `demodulate(iq, mode, state)` selects the above.
- A fresh `state` is created per receiver start; `demodulate` is callable on
  a single block with a fresh state for unit tests.

### 2. Wiring: `backend/app.py`

- Add a `"radio"` mode:
  - `_state` gains `radio` and `radio_config` slots; `RadioConfig` default in
    initial state.
  - `_start_radio(cfg)` follows `_start_adsb`: refuse if no device, stop all,
    bump generation, create `RadioReceiver`, store, set mode, start.
  - `on_audio` forwards PCM as a binary Socket.IO event `radio_audio`. An
    initial `radio_started` event carries
    `{ "sample_rate": 48000, "freq_mhz": <f>, "demod": <"fm"|"am"> }`.
  - `start_radio` socket handler (validate via `_filter_payload(data, RadioConfig)`).
  - Reuse the existing `stop` handler.
  - Add `radio` to `_stop_all_locked`'s slot list, the `connect` snapshot, and
    `_emit_status`.

### 3. Frontend: `index.html`, `waterfall.js`, `style.css`, new AudioWorklet

- New tab: `<button class="mode-tab" data-mode="radio">Radio</button>`.
- New control pane `pane-radio`:
  - AM/FM segmented toggle.
  - Frequency input (number, step 0.1 MHz) plus minus/plus step buttons.
  - Preset buttons (a few FM broadcast and a few airband AM).
  - Large Play / Stop control.
  - Volume slider.
  - Status line ("Playing 101.1 MHz FM" / "Stopped").
- New display view `view-radio`: minimal status (demod, tuned frequency,
  playing indicator).
- Audio playback:
  - On Play (a user gesture, so autoplay policy is satisfied): create an
    `AudioContext({ sampleRate: 48000 })`, add an `AudioWorklet` ring-buffer
    module, then emit `start_radio` with demod + frequency.
  - Incoming `radio_audio` binary frames: int16 to float32, pushed into the
    worklet ring buffer for glitch-free playback. Volume via a `GainNode`.
  - On Stop: emit `stop`, tear down the audio graph.
  - The AudioWorklet processor lives in `frontend/static/radio-audio-worklet.js`,
    loaded with `addModule`.

## Data flow

```
HackRF --(cs8 IQ)--> hackrf_transfer --stdout--> RadioReceiver._run
  -> demodulate(iq, "fm"|"am", state)  (numpy/scipy)
  -> int16 PCM @48k -> on_audio
  -> socketio.emit("radio_audio", pcm) -> browser
  -> int16 to float32 -> AudioWorklet ring buffer -> GainNode -> speakers
```

## Error handling

- `hackrf_transfer` missing: toast error, do not start.
- Device busy / not detected: `_start_radio` refuses with a toast.
- Subprocess dies unexpectedly: `on_exit("died")` -> generation-checked
  state reset + error toast (existing `_make_exit_handler` style).
- Browser `AudioContext` suspended: resumed on the Play click.

## Testing

- Unit tests (`tests/test_radio.py`, no hardware):
  - FM: synthesize IQ for a 1 kHz tone frequency-modulated onto the carrier,
    run `demod_fm`, assert the recovered audio's dominant frequency is about
    1 kHz.
  - AM: synthesize a baseband AM signal (carrier amplitude-modulated by a
    1 kHz tone), run `demod_am`, assert the recovered dominant frequency is
    about 1 kHz.
- On-device verification: tune a known local FM station and confirm audible
  audio; tune airband and confirm the AM path runs (audio when traffic is
  present). Confirm Stop frees the device and a subsequent Sweep works.

## File-by-file change list

- `backend/radio.py` (new): `RadioConfig`, `demod_fm`, `demod_am`,
  `demodulate`, `RadioReceiver`.
- `backend/app.py`: state slots, `_start_radio`, `start_radio` handler,
  `radio` in `_stop_all_locked` / connect snapshot / `_emit_status`,
  `radio_audio` and `radio_started` emits.
- `frontend/templates/index.html`: Radio tab, `pane-radio`, `view-radio`.
- `frontend/static/waterfall.js`: Radio controls, Web Audio playback wiring.
- `frontend/static/radio-audio-worklet.js` (new): ring-buffer AudioWorklet.
- `frontend/static/style.css`: styles for the Radio pane controls as needed.
- `tests/test_radio.py` (new): AM and FM demodulator unit tests.
- `pyproject.toml`: add scipy dependency (done).
- `README.md`: add Radio (AM/FM) to the modes table.
```
