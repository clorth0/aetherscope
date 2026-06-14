# Phase A: Data Foundation Design

**Status:** building on `feat/data-foundation`.

## Goal

Give Aetherscope a persistent data layer so it stops being a stateless receiver
and becomes a workflow tool: a curated bookmarks/frequency library (replacing the
hardcoded presets), settings that survive restarts, and annotatable captures.
This is the backbone later phases (logging, history, alerts, surveys) build on.

## Identity constraints

Self-hosted, offline, keyless, strict CSP, no new runtime dependency. Storage is
stdlib `sqlite3` (single file). One HackRF, one-job mutex is unchanged. All user
text is rendered through the existing `escapeHtml`. All SQL is parameterized.

## Module: `backend/store.py`

A small data-access layer.

- `Store(db_path: str | Path)` class so tests inject a temp path; module
  singleton `get_store()` opens the real DB lazily.
- Connection: `sqlite3.connect(path, check_same_thread=False)`, `row_factory =
  sqlite3.Row`, `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`. One shared
  connection guarded by a `threading.Lock` (low write volume, Socket.IO threading
  model). Every public method takes the lock.
- DB path: `AETHERSCOPE_DATA_DIR` env, default `~/.local/share/aetherscope/`
  (honor `XDG_DATA_HOME` if set). Directory created `0700`, DB file `0600`.
  Outside the repo so it never lands in git.
- Schema versioning: read `PRAGMA user_version`; apply ordered migrations greater
  than the current version; set `user_version`. v1 creates the three tables.

### Schema (v1)

```sql
CREATE TABLE bookmarks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  freq_hz INTEGER NOT NULL,
  demod TEXT,                       -- 'fm' | 'nfm' | 'am' | NULL
  label TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',    -- normalized comma-joined, e.g. "air,atc"
  source TEXT NOT NULL DEFAULT 'user', -- 'user' | 'seed' | 'mark'
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_heard_at INTEGER,
  hit_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL                -- JSON-encoded
);
CREATE TABLE captures (
  filename TEXT PRIMARY KEY,         -- basename of the .iq file
  user_label TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',
  last_played_at INTEGER
);
```

Timestamps are unix seconds (ints). The app passes `now` in (the DSP-free store
stays testable; no wall-clock calls buried inside).

## Validation (raise `ValueError` with a human message; the app turns it into a toast)

- `freq_hz`: 1_000_000 .. 6_000_000_000.
- `demod`: one of `fm`, `nfm`, `am`, or `None`.
- `label`: 1..80 chars after strip.
- `notes`: <= 500 chars.
- `tags`: <= 10 tags; each normalized to lowercase `[a-z0-9-]`, <= 24 chars;
  empties dropped; de-duplicated; stored comma-joined.

## Store API (all parameterized)

Bookmarks: `add_bookmark(now, freq_hz, demod, label, notes, tags, source) -> dict`,
`list_bookmarks() -> list[dict]` (tags split back to a list; ordered by label),
`update_bookmark(now, id, **fields) -> dict | None`, `delete_bookmark(id) -> bool`,
`bump_bookmark(now, id) -> dict | None` (sets `last_heard_at=now`, `hit_count+1`).

Settings: `get_setting(key, default=None)`, `set_setting(key, value)` (JSON
round-trip), `all_settings() -> dict`. Key allowlist enforced in the app, not the
store.

Captures: `get_capture_annotation(filename) -> dict`,
`upsert_capture_annotation(filename, user_label, notes, tags) -> dict`,
`touch_capture(now, filename)`, `delete_capture_annotation(filename) -> bool`.

Seeding: `seed_presets_once(now, presets) -> int` inserts the given preset list as
`source='seed'` bookmarks only if the `presets_seeded` setting is unset, then sets
it. Returns count inserted (0 on subsequent runs). Deleting seeded bookmarks does
not re-seed.

## App wiring (`backend/app.py`)

- On startup, `get_store()` then `seed_presets_once(now, PRESETS)` where `PRESETS`
  is the current hardcoded list (FM 93.9/97.1/99.5/101.1/105.9/107.6, AM airband
  118.3/121.5/124.0).
- `connect` payload gains `bookmarks` and `settings`.
- New socket handlers wrap the store and emit refreshed lists + a toast on error:
  `list_bookmarks`, `add_bookmark`, `update_bookmark`, `delete_bookmark`,
  `bump_bookmark`, `set_setting`. Settings keys allowlisted:
  `last_mode`, `last_radio_freq`, `last_demod`, `radio_volume`.
- `list_captures`: merge `capture.list_captures()` sidecar data with DB
  annotations by filename; flag entries whose `.iq` is missing as `missing: true`.
- `update_capture` handler; `delete_capture` also calls
  `delete_capture_annotation`.
- Validation errors from the store become error toasts; never 500.

## Front end

- `index.html` Radio pane: replace the static preset chips with a Bookmarks
  block (list with tune / edit / delete, an add row, a tag filter input).
- `waterfall.js`: bookmark CRUD over the new events; tuning a bookmark sets
  freq+demod and starts radio, then emits `bump_bookmark`; a tag filter; settings
  restore on load (apply `last_mode`/`last_radio_freq`/`last_demod`/`radio_volume`
  and persist via `set_setting` when they change); capture-list inline annotate;
  a "save as bookmark" button on the Sweep marks list (promote, `source='mark'`).
- All rendered bookmark/capture text escaped with `escapeHtml`.

## Testing (TDD, hardware-free): `tests/test_store.py`

Use a `Store` on a temp DB path. Cover: schema creates and is idempotent across
two `Store` opens; bookmark add/list/update/delete; `bump_bookmark` updates
stats; tag normalization (case, dupes, bad chars, cap at 10); validation rejects
out-of-range freq, bad demod, over-long label/notes; settings set/get/default
round-trip including JSON types; capture annotate upsert + delete; `seed_presets_once`
seeds once and is a no-op the second time even after a seeded row is deleted; a
bookmark label containing `'); DROP TABLE bookmarks;--` is stored and read back
verbatim (parameterized-query proof). Standalone `__main__` runner like the other
suites.

## Out of scope (later phases)

Capture audio/IQ-to-listen recording and SigMF (Phase B). A dedicated global
"Library" tab (bookmarks currently live in the Radio pane). Per-bookmark gain
fields. Event/contact logging tables (Phase C).
