# Phase B (part 2): Listen / Decode from a Saved IQ Capture

**Status:** building on `feat/iq-playback`.

## Goal

Make saved IQ captures usable, not just viewable: demodulate a `.iq` to audio
with an offset slider to tune within the captured band, and run rtl_433-style
data decoding over the file. Closes the capture loop.

## Identity / constraints

Reuse the proven `radio.demodulate` chain and the AudioWorklet audio path. No new
dependency (scipy already present for `resample_poly`; rtl_433 already used by
Decode mode). Playback/decode are read-only (no geotag/SigMF writes). Both are
device-free jobs (like IQ replay) but mutually exclusive via `_stop_all_locked`,
since they reuse the single audio path / event streams.

## Part A: Audio listen

### DSP core (pure, unit-tested) `backend/iq_playback.py`
- `shift_and_resample(iq, capture_rate, offset_hz, out_rate=FS_IN) -> np.ndarray`:
  mix by `exp(-j 2 pi offset/capture_rate n)` to bring the target signal to
  baseband, then `scipy.signal.resample_poly` from `capture_rate` to `out_rate`
  (=2 MHz, the demod input), which lowpasses to +/-1 MHz around the target.
- Continuous mixer phase across blocks (track sample index).
- Downstream uses the existing `radio.demodulate(iq, demod, state)` unchanged.

### Player `IqAudioPlayer`
- Reads the `.iq` in blocks, applies `shift_and_resample`, demodulates, emits
  `radio_audio` (binary) paced to real time, then a done event. Frequency model:
  captured band is `freq_hz +/- capture_rate/2`; `offset_hz` selects the signal.
- Reuses the radio client path: emit `radio_started {sample_rate: AUDIO_RATE,
  freq_mhz, demod}` so the AudioWorklet plays, then `radio_audio` frames.

### Caveat
Block-boundary resampling can cause minor clicks (stateless `resample_poly` per
block). Acceptable for v1; a stateful polyphase filter is the later refinement.

## Part B: Data decode (rtl_433 over the file)

- rtl_433 reads a file with `-r cs8:<path> -s <rate>`, but is tuned for
  ~250 kHz-1 MHz. Captures at 2-20 MSps are decimated first to a decoder-friendly
  rate (target ~1.024 MSps) via `resample_poly`, written to a temp cs8 file, then
  `rtl_433 -r cs8:<temp> -s <rate>` is run; JSON-per-line events are parsed and
  emitted on the existing `decode_event` channel (reuse the Decode panel).
- One-shot over the file (not real time); emits a done event. Temp file removed
  after.
- **Verification gate:** confirm it actually decodes a real ISM capture before
  shipping; if rtl_433-from-file proves unreliable on our format/rates, report and
  scope down rather than claim it works.

## Backend wiring (`app.py`)

- `play_capture {name, demod, offset_hz}`: validate name (no traversal; must be a
  `.iq` in `CAPTURES_DIR`), read `freq_hz`/`sample_rate` from the sidecar, start
  `IqAudioPlayer` (device-free, exclusive). Changing demod/offset restarts it.
- `decode_capture {name}`: start the file decode job; emits `decode_event`s + a
  done toast.
- Both go through `_stop_all_locked` and occupy a job slot so they are exclusive
  with live jobs and each other; stop via the existing `stop`.

## Front end

- IQ capture rows get **Listen** and **Decode** buttons (audio captures keep just
  Play; both new buttons are IQ-only).
- **Listen** opens the Radio view in a "playing from capture" state with a demod
  selector and an **offset slider** (`+/- capture_rate/2`); changing either
  restarts playback (`play_capture`). Reuses the now-playing widget + worklet.
- **Decode** switches to the Decode view and streams events into the existing
  events panel.
- All capture-derived strings escaped with `escapeHtml`.

## Testing

- `tests/test_iq_playback.py` (hardware-free): `shift_and_resample` recovers a
  tone placed at a known offset (after mix+resample the tone sits near baseband,
  verified via FFT peak); output length matches the rate ratio; offset 0 is a
  plain decimation.
- Live: capture a strong FM station wideband, Listen with offset tuned onto it,
  confirm audio. Capture ISM 433 and Decode the file; confirm device events
  appear (the Part B verification gate).

## Out of scope

Stateful seamless resampling; ADS-B/other decoders from file; scrubbing/seeking
within a capture during playback.
