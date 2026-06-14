# Auto-Tuner (Snap-to-Peak) Design

**Status:** scaffolding on `feat/auto-tuner`, untagged, for a future release.

## Goal

While listening to AM/FM/NBFM radio, let the user click one button to recenter
the tuner on the strongest carrier near the current frequency, so a slightly-off
or drifting tune snaps to the optimal frequency for that station.

## Why this is cheap

The radio already captures the full 2 MHz around the tuned frequency
(`hackrf_transfer` at `FS_IN`), so the station is already in the receiver's IQ.
Finding its true center is one FFT over a block we already have, plus a clean
restart of the receiver at the corrected frequency. No extra device contention,
no band sweep.

## Components

### `backend/tuning.py` (new) — pure DSP

```
find_peak_offset(iq, sample_rate, search_hz=100_000, guard_hz=5_000,
                 min_snr_db=6.0, fft_size=4096) -> float
```

- Remove the DC / LO-leakage spike: `iq - iq.mean()` (same reasoning as
  `radio.signal_dbfs`; otherwise the center artifact is always the peak).
- Averaged, fftshifted periodogram via `replay.iq_frame` (reused, DRY), giving
  dB power across `-sample_rate/2 .. +sample_rate/2`.
- Map bins to Hz: `f[k] = (k - N/2) * sample_rate / N`.
- Consider only `guard_hz <= |f| <= search_hz`:
  - `guard_hz` skips the center dead zone so residual LO leakage never wins.
  - `search_hz` bounds how far it may move, so it won't hop to an adjacent
    station.
- Take the strongest bin in that window. Return its Hz offset **only if** it is
  at least `min_snr_db` above the window's median power; otherwise return `0.0`
  (a dead band leaves tuning unchanged).
- `fft_size=4096` at 2 Msps gives ~488 Hz resolution: more than enough to center
  a station. Sub-bin interpolation is intentionally out of scope (YAGNI).

The function is total: empty input or an empty periodogram returns `0.0`.

### `backend/radio.py` — expose the latest block

`RadioReceiver` caches the most recent demodulated block's IQ in `self._last_iq`
(set once per loop) and exposes `latest_iq() -> np.ndarray | None`. A reference
swap is atomic in CPython, so the snap handler can read it from another thread
without a lock.

### `backend/app.py` — `snap_radio` socket handler

- Valid only in `radio` mode (not the scanner, not other jobs); otherwise an
  error toast.
- `iq = recv.latest_iq()`; if `None`, info toast "no samples yet."
- `search_hz` by mode: FM 50 kHz, AM/NFM 15 kHz. Snap **fine-centers the station
  you are on**, it does not seek nearby ones (seek is out of scope). The window
  stays well inside the channel spacing so a stronger neighbor, or its
  modulation skirt, cannot win. See "Hardware findings" below.
- `offset = find_peak_offset(iq, cfg.sample_rate_hz, search_hz=search_hz)`.
- `|offset| < 500 Hz` -> info toast "no stronger signal to snap to."
- Otherwise `new = round(cfg.freq_mhz + offset/1e6, 4)`, restart via
  `_start_radio(replace(cfg, freq_mhz=new))`, success toast
  "Snapped to <new> MHz (<+/-kHz>)." The existing `radio_started` event updates
  the displayed frequency, so no extra client state is needed.

## Hardware findings (HackRF, FM broadcast band)

Validated against live FM with the HackRF, which reshaped the search widths:

- A wide (+-100 kHz) FM window mis-snaps when tuned roughly between two stations:
  from 101.05 MHz it locked onto 100.952 MHz, the upper modulation skirt of the
  strong 100.9 station whose carrier sat just outside the window, instead of the
  real in-window 101.1 carrier. Narrowing FM to +-50 kHz fixes this: the same
  101.05 tune now no-ops (nothing clean in range) rather than grabbing a skirt,
  while a tune near a station (101.13) still centers it.
- Raw max-bin is stable across repeated captures and window widths. A smoothed
  PSD peak was tried and was *less* stable (it chased a neighbor's broad lump),
  so smoothing was dropped (YAGNI).
- Wideband FM is offset-tolerant and its averaged energy peak can sit ~20 kHz off
  the carrier, so FM snap is a harmless "good enough" centering. The feature is
  most precise for AM/NFM, whose discrete carrier is exactly the peak.

### Front end

- `frontend/templates/index.html`: a "Snap to signal" button beside the radio
  frequency input.
- `frontend/static/waterfall.js`: on click, `socket.emit("snap_radio")`. Toast
  and frequency display are handled by existing infrastructure.

## Testing (TDD, hardware-free)

`tests/test_tuning.py` builds synthetic IQ tones (same approach as
`tests/test_replay.py`):

1. Locks onto a +30 kHz tone (offset within one bin).
2. Ignores a dominant DC spike and still finds a weaker +20 kHz tone.
3. Returns `0.0` on pure noise (nothing clears `min_snr_db`).
4. Returns `0.0` when the only tone sits outside `search_hz`.
5. Resolves a negative offset (-40 kHz).

## Out of scope (future)

- Seek up/down (car-radio band browsing): a quick `hackrf_sweep` -> peak list ->
  jump to the next peak. Documented for a later branch.
- Sub-bin frequency interpolation.
