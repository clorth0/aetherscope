# Agent safety plan

This is an approval packet. The running app stays as it is. After this lands, the product changes below wait for the owner. Scripts in `prototypes/` measure the gaps and are safe to rerun. The app does not import them, and CI does not run them. A failing prototype exit code is the measurement.

Each proposed fix is the strongest change that removes the mistake. Structure first, then types, then a check, then a test. A paragraph in an agent guide is last, and only for a call a check cannot express. A mistake is listed here only when it happened twice.

GitHub has one open pull request, the Dependabot bump of werkzeug, and no review comments on the repository. The repeated corrections are commits on `main`.

## 1. CI runs 18 of 88 tests

`6f904ec` added `.github/workflows/ci.yml` with four named files: `test_sweep.py`, `test_radio.py`, `test_bands.py`, `test_capture.py`. The workflow file has not changed since. Eleven suites added before and after that commit are absent. `test_replay.py` (`7e2a577`) and `test_telemetry.py` (`137f79b`) already existed when CI was added and were left out. The later omissions are `test_tuning.py` (`beddbf5`), `test_store.py` (`754735e`), `test_gps.py` (`55d616e`), `test_sigmf.py` (`23ab9e7`), `test_audio_record.py` (`bf39239`), `test_iq_playback.py` (`b49b867`), `test_decode_file.py` (`52f5c11`), `test_adsb_enrich.py` (`a2a1ec0`), and `test_config.py` (`7c0e59d`).

`f93ed3e` and `9a9ddbf` both say the fifteen suites were green. That was a local run. CI still executes the original four files. The task that introduced CI, in `docs/superpowers/plans/2026-06-14-instrument-and-observability-upgrades.md`, says to run `tests/test_*.py`. The workflow does not.

`prototypes/check_ci_gap.py` prints the gap and exits 1 on this tree.

```
test files on disk: 15
test files named by .github/workflows/ci.yml: 4
function-style tests: 88
unittest.TestCase methods: 0
function-style tests in the CI list: 18
function-style tests CI does not run: 70
```

Two runners that look like they close the gap do not. This suite has zero `unittest.TestCase` methods, so `python -m unittest discover` collects nothing useful. `python tests/test_*.py` executes only the first path and puts the other paths in `argv`. Passing `tests/test_bands.py` and `tests/test_telemetry.py` together printed `4/4 passed` from the bands file. The telemetry file never ran.

The full loop was run on this machine as `for f in tests/test_*.py; do uv run python "$f"; done`. Three warm runs took 4718 ms, 4745 ms, and 4833 ms. All 15 files passed each time. A cold run before those took 6212 ms. The warm spread is 115 ms. Fifteen interpreter starts account for that time. The suites stay in separate processes because several of them mutate module globals such as the telemetry counters and the store singleton.

**Fix.** Make the directory the list. In CI, loop `uv run python` over `tests/test_*.py` and fail the job if any file fails. Apply the same idea to frontend syntax. Run `node --check` on every `frontend/static/*.js`, and leave `frontend/static/vendor/` out of that glob. After that change, the workflow names no individual test files.

A generated test index, or a switch to `unittest`, is a worse fix. The files are already executable, and discovery would go green while running zero tests.

## 2. One device job is copied, and the copies drift

The HackRF has one owner. `63a1e2e` put that rule in a reentrant lock, a generation counter, and `_stop_all_locked`. Every mode added since then pasted a new `_start_*` function. `backend/app.py` now has ten of them, 295 lines, plus a 30-line stop helper. The slot list is written twice, at the two `for slot in (...)` loops inside `_stop_all_locked`, and the two tuples are identical.

The frontend keeps a third copy. `refreshStatusUI` in `frontend/static/waterfall.js` names `sweep`, `decode`, `capture`, `adsb`, `scan`, `scan_radio`, and `replay`. Anything else becomes the text `Idle`. `prototypes/check_mode_labels.py` exits 1 on this tree and prints:

```
pill text falls through to Idle: decode_file iq_play radio
copied job-slot tuples: 2
the slot tuples are identical copies
```

`radio` has been missing from that chain since the radio mode landed in `a02321d`. `iq_play` and `decode_file` were added in `141dae0` and `52f5c11` and never got a label. While a listener is tuned to a station, the status pill says Idle. The mode comment above `_state` and the comment on `serverMode` are older still, and they omit modes the code actually sets.

The same copy habit shows up in the review fixes, each one patching a single caller:

- `d0ae388` and `3ad216e` stop `hackrf_info` from probing while a job owns the device. The guard is a check of `_state["mode"]` at each probe site.
- `810d42c` moves store reads out of `_state_lock` after a connect handler held the lock across SQLite.
- `f93ed3e` wraps `_start_capture` so a failed start cannot leave the app stopped with no way back. The other `_start_*` functions still assign `_state["mode"]` and then call `.start()` with no handler around the call.
- `f93ed3e` also tries to stop a torn read of the audio recorder. `on_connect` still reads `_audio_rec["record"]` once and `_audio_rec["recorder"]` separately, and neither read holds `_state_lock`. The comment describes the first read as the fix. The two fields can still disagree.
- `4aa44da` fixes the gpsd reader, which died when another thread closed its socket. The repair keeps a local socket reference on the reader thread.

**Two shapes.**

A mode table is the small shape. One tuple owns the slot names. The server sends `status_label` on the status event. `refreshStatusUI` prints that string and deletes the `if` chain. A check with the same logic as `prototypes/check_mode_labels.py` fails when a mode assignment has no label. This removes the Idle pill and the duplicated tuple. It leaves the ten start functions in place, so the next mode can still forget the generation check or leave `mode` set when `start()` throws.

One current job is the shape that removes the protocol. `_state` holds `current: Job | None` plus the persisted configs the UI already restores while idle. `Job` carries `kind`, `uses_device`, `status_label`, and `stop`. `start_job` stops the previous job, starts the new one, and publishes it only if `start()` returns. The device poller and `on_refresh_device` read `uses_device`. The exit path compares job identity. Adding a mode cannot forget a label or a slot, because both are fields the constructor requires.

**Choice.** Land the mode table first. It deletes a copied list, and it fixes the pill a user can see. Land one current job after that, and delete the ten `_start_*` bodies in that same change. The audio snapshot is a separate small fix on the way there. Under `_state_lock`, copy both recorder fields, then emit. That is the gpsd local-reference pattern applied to the recorder. It lands before the job collapse.

A class per mode would add types without deleting branches. Skip it.

## 3. Bounds sit beside the type, so the nearest example has none

Socket handlers build a dataclass from the payload and sometimes check it afterwards. The check is easy to skip because the object already exists.

`CaptureConfig` is the careful example, and it is still optional. `capture_config_error` was added in `3ad216e`. `f93ed3e` added another label bound. `prototypes/bounded_config_sketch.py` builds `CaptureConfig(duration_s=1e9)` successfully. `capture_config_error` then returns `Duration must be 0.1 to 600 seconds`. Any caller that forgets the function keeps the illegal object.

`RadioConfig` is checked only inside `on_start_radio`, for demod and for 1 to 6000 MHz. `on_snap_radio` calls `_start_radio` with `replace(...)` and does not repeat those checks. `SweepConfig`, `DecodeConfig`, `AdsbConfig`, and `ScanConfig` have no bounds. The sketch constructs all of these:

```
SweepConfig(f_start_mhz=-5, f_stop_mhz=99999, bin_width_hz=1, lna_gain=999)
DecodeConfig(freq_hz=-1, sample_rate=1, label=<500 x characters>)
RadioConfig(demod="usb", freq_mhz=1e9)
AdsbConfig(gain_db=-100, rx_lat=999, rx_lon=999)
```

The store learned the same lesson three times. `876679d` made seeding atomic and re-validated updates. `55e3878` stripped a bookmark label once so the stored value matched the check. `b93cd1e` added `validate_capture_annotation` and guarded `on_update_capture`. Validation that sits next to the write gets a second, looser caller.

Capture filenames repeat a traversal check five times, and the copies disagree. `_start_replay` rejects `/` and `..`. `_start_iq_play` and `_start_decode_file` also require a `.iq` suffix. `redact_location` and `delete_capture` reject `/` and `..`, then strip `.iq`, `.wav`, `.json`, or `.sigmf-meta`. An agent copying `_start_replay` into a new handler gets the weaker check.

`f93ed3e` also guarded `AETHERSCOPE_GPSD_PORT` at import after a bad value crashed startup. That is the same boundary, one step earlier, for an environment variable.

**Fix.** Put the bounds in `__post_init__` so the illegal config cannot be constructed. The handlers already catch `TypeError` and `ValueError` from `SweepConfig(**...)` and the other constructors, and they already toast the message. After the constructors raise, delete the extra checks in `on_start_radio` and delete `capture_config_error`. `tests/test_capture.py` currently builds illegal configs on purpose and asserts the helper returns an error. Change those tests to assert that the constructor raises. That is the behavior a caller can see.

Add one `capture_basename(name)` helper that rejects a separator or `..` and returns the stem. Call it from the five sites. Keep the per-caller suffix rule, because replay, delete, and IQ playback do not accept the same files. Do not invent a second validation pass inside the job once the constructor has run.

The sketch's `CheckedSweep.__post_init__` already refuses the illegal sweep. Promoting that pattern onto the real dataclasses is the change. The gain-step comments on `SweepConfig` have not been a repeated bug. Leave the step rules alone until a second incident shows up.

## 4. HTML is a string, and escaping is optional

`b76ef78` added `escapeHtml` after rtl_433 fields, ADS-B hex and flight, and capture labels were interpolated into `innerHTML`. A crafted signal or label could run script in the app origin and then drive the HackRF. `36461fd` coerced a bookmark id before it went into a `data-id` attribute. `f93ed3e` escaped `ev.rssi`, scan `band`, and `decoder_hint`, and guarded a `toFixed` crash on a non-numeric track.

`escapeHtml` is still a call the author has to remember. `frontend/static/waterfall.js` is 2454 lines. New panels copy the nearest template. `prototypes/check_html_templates.py` flags every template that contains an HTML tag and interpolates a value the template does not pass through `escapeHtml` or `encodeURIComponent`. It exits 1 on the parent of `b76ef78` with 12 templates, including `model`, `(a.hex || "").toUpperCase()`, `(a.flight || "").trim()`, `c.label || c.name`, and `p.decoder_hint`. It exits 1 on the parent of `f93ed3e` with 20 templates, including `rssi` and `p.decoder_hint`. It exits 1 on this tree with 19 templates.

A scan that only looks at `innerHTML =` misses those bugs. The dangerous templates are returned from helpers and joined later. Some of the 19 current hits are numbers, or variables escaped on the previous line. The checker still groups them with `model` and `decoder_hint`. That grouping is why review keeps finding the next field.

`prototypes/html_tag.mjs` shows the alternative. A tagged template escapes every value. `node prototypes/html_tag.mjs` prints the payload with the tag escaped, then the same payload leaked by a plain template. Markup the caller built on purpose goes through `raw()`.

**Fix.** Add that tagged template next to today's `escapeHtml`, and use it for every template that contains a tag. Pass the raw field in. Delete the `escapeHtml` call at that site so values are not escaped twice. A fragment that is already markup, such as a badge built as HTML, is wrapped in `raw()`. That wrapper is the one judgment in the migration, and it is visible at the call.

Turn `prototypes/check_html_templates.py` into a CI check in the same change, after the 19 templates are gone. Point it at `git:b76ef78^:frontend/static/waterfall.js` in the PR description and show it exit 1. Do not land the check while this tree still has 19 hits, or `main` goes red with no migration.

Replacing the whole file with a framework would also stop the bug, and it would throw away the canvas, the audio worklet, and the CSP story. The tag is the smaller boundary.

## 5. New backend modules land before the architecture page names them

`docs/architecture.md` is a hand-kept list of modules. Feature commits added the file. A later commit added the name. `prototypes/check_architecture_names.py --rev <sha>` exits 1 on these feature commits:

| Revision | Missing stems |
| --- | --- |
| `beddbf5` | `tuning` |
| `754735e` | `store`, `tuning` |
| `8b0c2f4^` | `store`, `tuning` |
| `55d616e` | `gps` |
| `23ab9e7` | `sigmf` |
| `bf39239` | `audio_record` |
| `b49b867` | `audio_record`, `iq_playback` |
| `52f5c11` | `audio_record`, `decode_file`, `iq_playback` |
| `a2a1ec0` | `adsb_enrich` |
| `f5f8928^` | `gps` |
| `879cb1f^` | `sigmf` |
| `1a1bbff^` | `audio_record`, `decode_file`, `iq_playback` |
| `5de114a^` | `adsb_enrich` |

The repair commits are `8b0c2f4`, `f5f8928`, `879cb1f`, `1a1bbff`, and `5de114a`. On this tree the checker exits 0. `fe9a386` updated the store paragraph to mention the contact inventory. The stem `store` was already on the page, so the filename check passes on `fe9a386^`. The new sentence is a judgment about what the module does. The check covers a missing filename.

**Fix.** In CI, fail when a `backend/*.py` stem other than `__init__` is absent from `docs/architecture.md`. Generating the overview from docstrings would remove the list and replace the prose the page is for. The check forces the feature commit to contain the name. The paragraph stays a person's sentence.

## Ranked by what the fix removes

1. **Run every test file from the directory.** One loop. Seventy tests that already pass start running on every push. Every later suite is covered without another edit to the workflow.
2. **One current job, after a mode table.** Removes the copied start protocol, the duplicated slot list, and the Idle pill. This is the largest structural deletion in the packet.
3. **Bounds inside the config constructor, and one capture-name helper.** An illegal payload cannot be built, so a new handler cannot forget the check. The five filename copies collapse to one traversal rule.
4. **A tagged HTML template, then the template check in CI.** Closes the XSS class that three review commits patched field by field.
5. **The architecture filename check.** Stops the follow-up doc commit. Lower impact than the four above, and it is a repeated miss with a one-line check.

## Order to land after approval

Each item is its own change, proven before the next one starts. The suite loop is first so the later changes run the tests that exist today.

1. **CI loop.** Replace the four named test files with a loop over `tests/test_*.py`. Glob `frontend/static/*.js` for `node --check`. Rerun `prototypes/check_ci_gap.py` and expect exit 0. The three warm runs above are the timing evidence. No product code changes.
2. **Architecture names.** Add `prototypes/check_architecture_names.py` to CI, or move that check into the workflow. Show it failing on `55d616e` and passing on this tree.
3. **Constructor bounds.** `__post_init__` on the config dataclasses that cross the socket. Update `tests/test_capture.py`. Delete `capture_config_error` and the duplicate radio checks. Add `capture_basename` and switch the five call sites. Existing handler `try` / `except ValueError` stays.
4. **Audio snapshot.** In `on_connect`, copy both `_audio_rec` fields while holding `_state_lock`, then emit. This is the unfinished part of `f93ed3e`.
5. **Mode table.** One slot list. Server-sent `status_label`. Delete the status `if` chain. Wire `prototypes/check_mode_labels.py` into CI. Confirm a radio status event no longer falls through to Idle.
6. **One current job.** Replace the ten starters with `start_job`. Delete the old functions in the same change. Device probes read `uses_device`.
7. **Tagged HTML.** Add `html` and `raw`, migrate the 19 templates, then run `prototypes/check_html_templates.py` in CI. Show the checker exiting 1 on `git:b76ef78^:frontend/static/waterfall.js`.

## Do not add an agent guide yet

`AGENTS.md` does not exist, and this packet should not create it. A paragraph nobody is forced to open will not stop the next copy. Add the guide only after the checks above are in CI, and keep it to this table.

| Mistake | What enforces it | Guide text |
| --- | --- | --- |
| A new test file never runs | CI loops over `tests/test_*.py` | none |
| A new mode forgets a label or a slot | One job value, and `check_mode_labels.py` | none |
| A socket payload skips its bounds | Dataclass `__post_init__` | none |
| A new HTML field skips `escapeHtml` | `html` template and `check_html_templates.py` | `raw()` only for markup the caller already built |
| A new module skips the architecture page | CI name check | none |

These incidents do not earn a paragraph.

- `f93ed3e` changed `cors_allowed_origins` from `[]` to `None`, because an empty list disables the origin check. That happened once. A test that an unset `AETHERSCOPE_ALLOWED_ORIGINS` leaves the value as `None` is the right lock. Add it with the CI loop change if the test is cheap. Do not write a rule about it.
- `c21fa5a` bumped `pyproject.toml` and left `uv.lock` stale. `473f7ce` synced the lock. Later version bumps touched the lock in the same commit. One incident, and it stopped. No rule.
- `11e7559` stopped stale frontend assets with `APP_VERSION` and `Cache-Control: no-store`. The class is closed. No rule.
- `65a3fe7` removed em dashes from the architecture page. Later commits put the character back in comments and in the README. A character lint could ban it. That is optional, and it is outside this packet. Do not add a style paragraph.
- `e208f58` narrowed snap search after FM hardware testing, from a wide window that grabbed the neighbor station. The widths live next to `find_peak_offset` in `on_snap_radio`. That is a measured radio judgment. Leave it in the code.

## How to rerun the evidence

From the repo root:

```sh
python3 prototypes/check_ci_gap.py
python3 prototypes/check_mode_labels.py
python3 prototypes/check_architecture_names.py
python3 prototypes/check_architecture_names.py --rev 55d616e
python3 prototypes/check_html_templates.py
python3 prototypes/check_html_templates.py git:b76ef78^:frontend/static/waterfall.js
node prototypes/html_tag.mjs
uv run python prototypes/bounded_config_sketch.py
```

The checkers exit 1 while the gap they measure is still open. `check_architecture_names.py` with no revision exits 0 on this tree. `html_tag.mjs` and `bounded_config_sketch.py` exit 0. They demonstrate the replacement, and they are not wired in.
