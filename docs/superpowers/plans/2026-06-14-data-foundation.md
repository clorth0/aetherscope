# Phase A: Data Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent SQLite data layer for bookmarks, settings, and capture annotations, replacing hardcoded radio presets and surviving restarts.

**Architecture:** A single stdlib-`sqlite3` module (`backend/store.py`) with a `Store(db_path)` class and a `get_store()` singleton. The Flask/Socket.IO app wires CRUD socket events to it, seeds presets on startup, and merges capture sidecars with DB annotations. The front end gets a bookmarks list in the Radio pane, capture annotation, mark promotion, and settings restore.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`, Flask-SocketIO (threading), vanilla-JS front end. Tests are standalone `__main__` runners (`uv run python tests/test_*.py`), no pytest.

Reference design: `docs/superpowers/specs/2026-06-14-data-foundation-design.md`. Read it before starting any task.

Conventions to match:
- Run a single suite: `uv run python tests/test_store.py`. Expected output ends with `N/N passed` and exit 0.
- Commits: end with `Co-Authored-By: Matt James <clorth0@users.noreply.github.com>`, no Claude co-author line.
- All SQL parameterized (`?` placeholders), never f-string/format into SQL.
- All user text rendered in JS goes through the existing `escapeHtml`.

---

### Task 1: `backend/store.py` + `tests/test_store.py` (the whole store, TDD)

**Files:**
- Create: `backend/store.py`
- Create: `tests/test_store.py`

Build the store TDD: write each test, watch it fail, implement the minimal method, watch it pass, then move on. Commit once at the end of the task (the suite is the unit).

**Store surface (signatures):**
```python
class Store:
    def __init__(self, db_path): ...          # connect, WAL, row_factory, run migrations
    # bookmarks
    def add_bookmark(self, now, freq_hz, demod, label, notes="", tags=(), source="user") -> dict
    def list_bookmarks(self) -> list[dict]     # tags as list[str]; ordered by lower(label)
    def update_bookmark(self, now, id, **fields) -> dict | None   # subset of freq_hz/demod/label/notes/tags
    def delete_bookmark(self, id) -> bool
    def bump_bookmark(self, now, id) -> dict | None               # last_heard_at=now, hit_count+=1
    # settings
    def get_setting(self, key, default=None)
    def set_setting(self, key, value) -> None  # JSON round-trip
    def all_settings(self) -> dict
    # captures
    def get_capture_annotation(self, filename) -> dict            # zero-value dict if absent
    def upsert_capture_annotation(self, filename, user_label="", notes="", tags=()) -> dict
    def touch_capture(self, now, filename) -> None                # last_played_at=now (upsert)
    def delete_capture_annotation(self, filename) -> bool
    # seeding
    def seed_presets_once(self, now, presets) -> int              # presets: list of dict(freq_hz,demod,label)
```

Validation lives in module functions so they are independently testable:
```python
def normalize_tags(tags) -> str        # lowercase [a-z0-9-], drop empties/dupes, cap 10 each <=24, comma-join
def validate_bookmark(freq_hz, demod, label, notes) -> None   # raise ValueError(human msg) on bad input
```
Bounds: freq 1_000_000..6_000_000_000; demod in {fm,nfm,am,None}; label 1..80 after strip; notes <=500.

**Schema:** exactly the v1 DDL from the spec. Migrations keyed off `PRAGMA user_version`; running `Store` twice on the same path must not error or duplicate.

- [ ] **Step 1:** Write `tests/test_store.py` header (sys.path insert like `tests/test_replay.py`, import from `backend.store`, a `_store()` helper that returns `Store` on a `tempfile` path) and the first test `test_schema_is_idempotent` (open `Store` twice on the same path, no error, `list_bookmarks() == []`).
- [ ] **Step 2:** Run `uv run python tests/test_store.py` -> FAIL (no module). Implement `Store.__init__` + migrations + `list_bookmarks` minimal. Run -> PASS.
- [ ] **Step 3:** Add tests then implementations, one behavior at a time, watching each fail then pass:
  - `test_add_and_list_bookmark` (round-trips fields; tags come back as a list; `source` recorded; `hit_count==0`).
  - `test_update_bookmark` (changes a subset; `updated_at` advances; returns updated row; unknown id -> None).
  - `test_delete_bookmark` (true then false on missing).
  - `test_bump_bookmark` (`last_heard_at` set, `hit_count` increments).
  - `test_tag_normalization` (`["AIR","air"," ATC ","b@d!"] -> "air,atc,bd"`; >10 capped; each <=24).
  - `test_validation_rejects_bad_input` (freq 500_000, freq 7e9, demod "ssb", empty label, 81-char label, 501-char notes each raise ValueError).
  - `test_settings_roundtrip` (`set_setting("v",80)`/get ==80; dict and string values survive JSON; missing key returns default; `all_settings` returns the map).
  - `test_capture_annotation` (`get_capture_annotation` absent -> zero dict; upsert then get; upsert again updates; `touch_capture` sets `last_played_at`; delete true then false).
  - `test_seed_presets_once` (first call inserts len(presets) with source='seed' and returns that count; delete one seeded row; second call returns 0 and does not re-add).
  - `test_parameterized_queries_are_safe` (add a bookmark with label `"'); DROP TABLE bookmarks;--"`, then `list_bookmarks()` still works and the label reads back verbatim).
- [ ] **Step 4:** Run the full suite -> all PASS, output pristine.
- [ ] **Step 5:** Commit.
```bash
git add backend/store.py tests/test_store.py
git commit -m "Add SQLite store: bookmarks, settings, capture annotations (TDD)"
```

---

### Task 2: Wire the store into `backend/app.py`

**Files:**
- Modify: `backend/app.py`
- Modify: `backend/capture.py` (only if a small helper is needed; prefer not to)

Depends on Task 1.

- [ ] **Step 1:** Import `get_store` and add a module-level `PRESETS` list (current hardcoded chips: FM 93.9/97.1/99.5/101.1/105.9/107.6, AM 118.3/121.5/124.0 as `{"freq_hz":..., "demod":"fm"|"am", "label":"101.1"|"Air 118.3"}`). In `main()` startup (before serving), call `get_store().seed_presets_once(int(time.time()), PRESETS)` inside try/except (log on failure, never crash startup).
- [ ] **Step 2:** Extend the `connect` payload with `"bookmarks": store.list_bookmarks()` and `"settings": store.all_settings()`.
- [ ] **Step 3:** Add socket handlers, each guarded so a `ValueError` from the store becomes `_emit_toast("error", str(e))` and never a 500, then re-emit the fresh list:
  - `list_bookmarks` -> emit `bookmarks` `{ "data": [...] }`.
  - `add_bookmark(data)` -> validate via store, insert (`source` defaults `"user"`, allow `"mark"`), emit `bookmarks` + success toast.
  - `update_bookmark(data)` / `delete_bookmark(data)` / `bump_bookmark(data)` -> mutate, emit `bookmarks`.
  - `set_setting(data)` -> allowlist keys {`last_mode`,`last_radio_freq`,`last_demod`,`radio_volume`}; ignore others; persist.
- [ ] **Step 4:** Enrich `list_captures` handler: merge `capture.list_captures()` with `store.get_capture_annotation(name)` per file (add `user_label`,`notes`,`tags`); set `"missing": true` when the `.iq` is gone. Add `update_capture(data)` -> `upsert_capture_annotation`. In `delete_capture`, also `store.delete_capture_annotation(name)`.
- [ ] **Step 5:** Verify the backend imports and a socket smoke test passes:
```bash
uv run python -c "import backend.app; print('ok')"
```
Then a python-socketio client (pattern from earlier `/tmp` tests, run with `uv run --with python-socketio --with requests --with websocket-client`): connect, read `bookmarks` from the connect payload (seeded presets present), `add_bookmark`, confirm it appears, `delete_bookmark`, confirm gone, `set_setting`. Use a temp `AETHERSCOPE_DATA_DIR` so the test does not touch the real DB.
- [ ] **Step 6:** Run the whole test suite (`for t in tests/test_*.py; do uv run python "$t"; done`) -> all green. Commit.
```bash
git add backend/app.py
git commit -m "Wire store into app: bookmark CRUD, settings, capture annotations"
```

---

### Task 3: Bookmarks UI in the Radio pane

**Files:**
- Modify: `frontend/templates/index.html` (replace the static `#radio-presets` chips with a Bookmarks block)
- Modify: `frontend/static/waterfall.js`

Depends on Task 2 (event contract). Match existing markup/classes (`.panel`, `.band-buttons`, `.btn`, `lore`-style is N/A here; reuse current radio classes). Escape all bookmark text with `escapeHtml`.

- [ ] **Step 1:** In `index.html`, replace the hardcoded preset chips with a Bookmarks section: a tag-filter `<input>`, a `<div id="bookmark-list">`, and an "add current" affordance (label input + add button that uses the current radio freq/demod).
- [ ] **Step 2:** In `waterfall.js`: keep a `bookmarks` array, populated from the connect payload and refreshed on the `bookmarks` event. `renderBookmarks()` builds the list (each row: label, freq, demod badge, tune / edit / delete). Tune sets `radioFreqEl.value` + demod, starts radio (reuse the start-radio path), and emits `bump_bookmark{id}`. Apply the tag filter client-side.
- [ ] **Step 3:** Add/edit/delete emit `add_bookmark`/`update_bookmark`/`delete_bookmark`. The add control sends `{freq_hz: Math.round(parseFloat(radioFreqEl.value)*1e6), demod: radioDemod, label}`.
- [ ] **Step 4:** Verify in a headless browser (Playwright pattern from earlier): load page, open Radio tab, confirm seeded presets render as bookmarks, screenshot `#pane-radio`. Confirm no console errors and the page is not broken.
- [ ] **Step 5:** Commit.
```bash
git add frontend/templates/index.html frontend/static/waterfall.js
git commit -m "Add bookmarks list to the Radio pane (replaces hardcoded presets)"
```

---

### Task 4: Settings restore, promote-mark, capture annotation (front end)

**Files:**
- Modify: `frontend/static/waterfall.js`
- Modify: `frontend/templates/index.html` (marks list action + capture list fields)

Depends on Tasks 2-3.

- [ ] **Step 1:** Settings restore: on receiving the connect `settings`, apply `last_mode` (switch tab), `last_radio_freq`/`last_demod` (set radio inputs), `radio_volume` (set slider). When the user changes mode/freq/demod/volume, emit `set_setting`. Guard against feedback loops (only persist on user action).
- [ ] **Step 2:** Promote mark: on the Sweep marks list, add a "save as bookmark" button per mark that emits `add_bookmark{freq_hz, demod:null, label:<freq>, source:"mark"}`.
- [ ] **Step 3:** Capture annotate: in the capture list rendering, show `user_label`/`notes`/`tags` and an inline edit that emits `update_capture{filename,user_label,notes,tags}`; show a `missing` badge when `missing:true`.
- [ ] **Step 4:** Verify in headless browser: settings persist across a reload (set volume, reload, slider restored), a mark promotes into bookmarks, a capture annotation round-trips. Screenshot where useful.
- [ ] **Step 5:** Commit.
```bash
git add frontend/static/waterfall.js frontend/templates/index.html
git commit -m "Restore settings on load, promote marks, annotate captures"
```

---

### Task 5: gitignore, docs, integration verification

**Files:**
- Modify: `.gitignore` (add the default data dir name if it could land in repo, and `*.db`, `*.db-wal`, `*.db-shm`)
- Modify: `docs/architecture.md` (add `store.py` to the layout + a line on the data layer)

- [ ] **Step 1:** Add `*.db`, `*.db-wal`, `*.db-shm`, and `data/` to `.gitignore`. Confirm `git status` shows no stray DB artifacts.
- [ ] **Step 2:** Update `docs/architecture.md`: add `store.py` (SQLite data layer: bookmarks, settings, capture annotations) to the backend layout and one sentence in the overview.
- [ ] **Step 3:** Full integration: restart the launchd service (`launchctl kickstart -k gui/$(id -u)/local.aetherscope`), confirm `http://127.0.0.1:8765/` serves, and run the socket smoke test against the live service (bookmarks seeded, add/delete works). Confirm no stray RF process and the device stays connected.
- [ ] **Step 4:** Run the full suite once more -> all green. Commit.
```bash
git add .gitignore docs/architecture.md
git commit -m "Gitignore DB artifacts; document the data layer"
```

---

## Self-review notes

- Spec coverage: store (T1), app wiring incl. seed + capture merge (T2), bookmarks UI (T3), settings/promote/annotate (T4), gitignore/docs/verify (T5). All spec sections map to a task.
- Type consistency: `list_bookmarks()` returns tags as `list`; the store stores them comma-joined; the app emits `{data:[...]}` on the `bookmarks` event; the client reads `msg.data`. Settings values are JSON in the DB and plain JS values on the wire.
- No prod-DB risk: tests and the socket smoke tests set a temp `AETHERSCOPE_DATA_DIR`; the live verification uses the real DB intentionally (read + reversible add/delete only).
