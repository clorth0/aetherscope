# Phase B (part 1): WAV Audio Recording Design

**Status:** building on `feat/audio-record`.

## Goal

Record the demodulated audio you are listening to (Radio mode) to a WAV file,
stored and managed alongside IQ captures, geotagged like them. First half of the
"close the loop" phase; listen/decode-from-IQ is the second, separate piece.

## Identity / constraints

No new dependency (stdlib `wave`). Geotagging stays opt-in and is reused as-is.
The live capture/replay flow is untouched. SigMF intentionally does not apply
(SigMF describes complex IQ, not audio); the geotag rides in the JSON sidecar.

## Module: `backend/audio_record.py`

- `WavRecorder(path, rate, channels=1, sampwidth=2)`: stdlib `wave` writer.
  `write(pcm: bytes)` appends int16 LE PCM, `close()` finalizes, thread-safe via
  a lock, tracks total frames. `frames` / `duration_s` (frames / rate) and
  `file_size` available after writing.
- `AudioRecord` dataclass (sidecar shape), mirrors `CaptureRecord` enough for the
  capture list to render it:
  `kind="audio"`, `name` (`<base>.wav`), `path`, `sidecar`, `freq_hz`, `demod`,
  `audio_rate`, `started_at`, `finished_at`, `file_size`, `duration_s`, `label`,
  `geolocation`.

Captures land in `CAPTURES_DIR` (same as IQ), filename
`<YYYY-MM-DD_HH-MM-SS>_<label-or-freq>.wav` + `.json` sidecar.

## `backend/app.py`

- Module state `_audio_rec` (active `WavRecorder` + its `AudioRecord`) guarded by
  `_state_lock`.
- The radio `on_audio` lambda becomes `_on_radio_audio(pcm)`: emit `radio_audio`
  (unchanged) and, if recording, `WavRecorder.write(pcm)`.
- `start_audio_record {label}`: only valid while a radio receiver is running
  (else error toast). Builds the path, reads `_gps.geolocation()` coarsened by
  `gps_precision`, writes the initial sidecar, sets `_audio_rec`, emits
  `audio_record_status {recording: true, name}`.
- `stop_audio_record`: close the recorder, rewrite the sidecar with
  finished_at/duration/size, clear `_audio_rec`, emit `audio_record_status
  {recording: false}` + the refreshed capture list + a toast.
- Auto-stop: when the radio stops or dies (radio `on_exit` and `_stop_all_locked`),
  stop any active recording so a WAV is always finalized.
- Connect snapshot includes `audio_record_status`.
- `_enriched_captures`: the `missing` check and SigMF flag key off the sidecar's
  `name` extension (`.iq` vs `.wav`); `kind` (default "iq") is passed through;
  `sigmf` only for `.iq`.

## `backend/capture.py`

- `delete_capture` also removes `<base>.wav`.
- `list_captures` already reads any `*.json` sidecar and refreshes `file_size`
  from `path`; works for audio unchanged.
- `redact_location` operates on the `.json` sidecar (nulls `geolocation`); works
  for audio unchanged.

## Front end

- Radio controls: a **Record** toggle button (next to Listen/Snap/Stop) with a
  recording indicator; reflects `audio_record_status`. Disabled/ignored unless
  the radio is playing.
- Capture list: entries with `kind === "audio"` show an **audio** badge; actions
  are **Play** (browser-native `<audio>`/`new Audio("/captures/<name>.wav")`,
  no DSP), Download, Edit, Remove location (if geotagged), Delete. The
  spectrogram **Replay** and the **SigMF** link render only for `.iq` captures.
- All capture/recording text escaped with `escapeHtml`.

## Privacy

Audio recordings are geotagged exactly like IQ captures (opt-in GPS, precision
coarsening, per-capture redaction). The WAV carries no embedded location; the
geotag is sidecar-only.

## Testing (TDD where it applies): `tests/test_audio_record.py`

- `WavRecorder` writes a valid WAV: reopen with `wave`, assert nchannels=1,
  sampwidth=2, framerate, and frame count == bytes/2.
- `duration_s` == frames / rate; `file_size` > 0 after writing.
- Writing in two chunks accumulates frames.
- Live: record ~3 s of FM, confirm a playable `.wav` + sidecar (+ geotag when GPS
  on), appearing in the capture list with an audio badge, plays in the browser.

## Out of scope (next piece)

Listen/decode from a saved IQ capture (the DSP-heavier half: decimation to the
demod rate + offset tuning).
