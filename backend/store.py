"""SQLite data-access layer for Aetherscope.

Public API
----------
normalize_tags(tags) -> str
validate_bookmark(freq_hz, demod, label, notes) -> None
Store(db_path)
get_store() -> Store   (module singleton)
"""

import json
import os
import re
import sqlite3
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Module-level validation / normalisation helpers
# ---------------------------------------------------------------------------

_ALLOWED_DEMODS = {"fm", "nfm", "am", None}


def normalize_tags(tags) -> str:
    """Accept list/tuple or comma-string; return clean, comma-joined string.

    Rules applied per tag (in order):
      strip -> lowercase -> keep [a-z0-9-] only -> truncate to 24 chars ->
      drop empties -> de-duplicate (first occurrence wins) -> cap at 10 tags.
    """
    if isinstance(tags, str):
        raw = tags.split(",")
    else:
        raw = list(tags)

    seen: list[str] = []
    for t in raw:
        t = t.strip().lower()
        t = re.sub(r"[^a-z0-9-]", "", t)
        t = t[:24]
        if t and t not in seen:
            seen.append(t)
        if len(seen) == 10:
            break
    return ",".join(seen)


def validate_bookmark(freq_hz, demod, label, notes) -> None:
    """Raise ValueError with a short human message on invalid bookmark fields.

    Bounds:
      freq_hz : int in [1 000 000, 6 000 000 000]
      demod   : "fm" | "nfm" | "am" | None
      label   : 1..80 chars after strip
      notes   : <= 500 chars
    """
    if not isinstance(freq_hz, int) or isinstance(freq_hz, bool) \
            or not (1_000_000 <= freq_hz <= 6_000_000_000):
        raise ValueError(
            f"freq_hz must be an int in [1 MHz, 6 GHz]; got {freq_hz!r}"
        )
    if demod not in _ALLOWED_DEMODS:
        raise ValueError(
            f"demod must be one of {sorted(d for d in _ALLOWED_DEMODS if d)} or None; got {demod!r}"
        )
    if not isinstance(label, str) or not (1 <= len(label.strip()) <= 80):
        raise ValueError("label must be 1–80 non-whitespace chars")
    if not isinstance(notes, str) or len(notes) > 500:
        raise ValueError("notes must be a string of at most 500 chars")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmarks (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  freq_hz      INTEGER NOT NULL,
  demod        TEXT,
  label        TEXT    NOT NULL,
  notes        TEXT    NOT NULL DEFAULT '',
  tags         TEXT    NOT NULL DEFAULT '',
  source       TEXT    NOT NULL DEFAULT 'user',
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL,
  last_heard_at INTEGER,
  hit_count    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS captures (
  filename      TEXT PRIMARY KEY,
  user_label    TEXT    NOT NULL DEFAULT '',
  notes         TEXT    NOT NULL DEFAULT '',
  tags          TEXT    NOT NULL DEFAULT '',
  last_played_at INTEGER
);
"""

# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _bm_dict(row) -> dict:
    """Convert a bookmarks sqlite3.Row to a plain dict, tags as list."""
    d = dict(row)
    raw = d.get("tags") or ""
    d["tags"] = [t for t in raw.split(",") if t]
    return d


def _cap_dict(row) -> dict:
    """Convert a captures sqlite3.Row to a plain dict, tags as list."""
    d = dict(row)
    raw = d.get("tags") or ""
    d["tags"] = [t for t in raw.split(",") if t]
    return d


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    """Thread-safe SQLite wrapper for Aetherscope persistence."""

    def __init__(self, db_path):
        db_path = Path(db_path)
        parent = db_path.parent
        if not parent.exists():
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)

        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Reentrant so methods that take the lock can call other locked methods
        # (e.g. seed_presets_once wraps add_bookmark/get_setting/set_setting in
        # one atomic critical section).
        self._lock = threading.RLock()

        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")

            # Best-effort: restrict DB file permissions
            try:
                os.chmod(db_path, 0o600)
            except OSError:
                pass

            # Run migrations atomically: DDL + the version bump in one
            # transaction, so a crash mid-migration cannot leave the schema
            # half-created with a stale user_version. sqlite3's implicit
            # transaction handling does not wrap executescript, so drive the
            # statements explicitly. (foreign_keys/journal_mode pragmas above
            # do not open a transaction, so BEGIN is valid here.)
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                try:
                    self._conn.execute("BEGIN")
                    for stmt in filter(str.strip, _V1_SCHEMA.split(";")):
                        self._conn.execute(stmt)
                    self._conn.execute("PRAGMA user_version = 1")
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise

    # ------------------------------------------------------------------
    # Bookmarks
    # ------------------------------------------------------------------

    def add_bookmark(
        self,
        now: int,
        freq_hz: int,
        demod,
        label: str,
        notes: str = "",
        tags=(),
        source: str = "user",
    ) -> dict:
        """Validate, insert, and return the new bookmark row as a dict."""
        if isinstance(label, str):
            label = label.strip()  # strip once; stored value matches what we validate
        validate_bookmark(freq_hz, demod, label, notes)
        tags_str = normalize_tags(tags)
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO bookmarks
                   (freq_hz, demod, label, notes, tags, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (freq_hz, demod, label, notes, tags_str, source, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM bookmarks WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return _bm_dict(row)

    def list_bookmarks(self) -> list:
        """Return all bookmarks ordered by lower(label)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bookmarks ORDER BY lower(label)"
            ).fetchall()
        return [_bm_dict(r) for r in rows]

    def update_bookmark(self, now: int, id: int, **fields) -> dict | None:
        """Update allowed fields on bookmark *id*; return updated dict or None."""
        allowed = {"freq_hz", "demod", "label", "notes", "tags"}
        updates = {k: v for k, v in fields.items() if k in allowed}

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bookmarks WHERE id = ?", (id,)
            ).fetchone()
            if row is None:
                return None
            current = dict(row)

            # Merge caller-supplied values with current values
            merged_freq  = updates.get("freq_hz", current["freq_hz"])
            merged_demod = updates.get("demod",   current["demod"])
            merged_label = updates.get("label",   current["label"])
            merged_notes = updates.get("notes",   current["notes"])
            if isinstance(merged_label, str):
                merged_label = merged_label.strip()  # strip once; matches what we store

            # Re-validate the resulting bookmark
            validate_bookmark(merged_freq, merged_demod, merged_label, merged_notes)

            if "tags" in updates:
                tags_str = normalize_tags(updates["tags"])
            else:
                tags_str = current["tags"]

            self._conn.execute(
                """UPDATE bookmarks
                   SET freq_hz=?, demod=?, label=?, notes=?, tags=?, updated_at=?
                   WHERE id=?""",
                (merged_freq, merged_demod, merged_label,
                 merged_notes, tags_str, now, id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM bookmarks WHERE id = ?", (id,)
            ).fetchone()
        return _bm_dict(row)

    def delete_bookmark(self, id: int) -> bool:
        """Delete bookmark by id. Returns True if a row was deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM bookmarks WHERE id = ?", (id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def bump_bookmark(self, now: int, id: int) -> dict | None:
        """Increment hit_count and set last_heard_at=now. Returns None if absent."""
        with self._lock:
            cur = self._conn.execute(
                """UPDATE bookmarks
                   SET last_heard_at = ?, hit_count = hit_count + 1
                   WHERE id = ?""",
                (now, id),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM bookmarks WHERE id = ?", (id,)
            ).fetchone()
        return _bm_dict(row)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def get_setting(self, key: str, default=None):
        """Return JSON-decoded value for *key*, or *default* if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def set_setting(self, key: str, value) -> None:
        """JSON-encode *value* and upsert into settings."""
        encoded = json.dumps(value)
        with self._lock:
            self._conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, encoded),
            )
            self._conn.commit()

    def all_settings(self) -> dict:
        """Return {key: decoded_value} for every row in settings."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM settings"
            ).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    # ------------------------------------------------------------------
    # Captures
    # ------------------------------------------------------------------

    def get_capture_annotation(self, filename: str) -> dict:
        """Return annotation dict for *filename*; zero-value dict if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM captures WHERE filename = ?", (filename,)
            ).fetchone()
        if row is None:
            return {
                "filename": filename,
                "user_label": "",
                "notes": "",
                "tags": [],
                "last_played_at": None,
            }
        return _cap_dict(row)

    def upsert_capture_annotation(
        self,
        filename: str,
        user_label: str = "",
        notes: str = "",
        tags=(),
    ) -> dict:
        """Insert or update capture annotation (tags normalized). Returns row dict."""
        tags_str = normalize_tags(tags)
        with self._lock:
            self._conn.execute(
                """INSERT INTO captures (filename, user_label, notes, tags)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(filename) DO UPDATE SET
                     user_label = excluded.user_label,
                     notes      = excluded.notes,
                     tags       = excluded.tags""",
                (filename, user_label, notes, tags_str),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM captures WHERE filename = ?", (filename,)
            ).fetchone()
        return _cap_dict(row)

    def touch_capture(self, now: int, filename: str) -> None:
        """Set last_played_at=now; create a minimal row if the file is absent."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO captures (filename, last_played_at)
                   VALUES (?, ?)
                   ON CONFLICT(filename) DO UPDATE SET last_played_at = excluded.last_played_at""",
                (filename, now),
            )
            self._conn.commit()

    def delete_capture_annotation(self, filename: str) -> bool:
        """Delete capture annotation. Returns True if a row was deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM captures WHERE filename = ?", (filename,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def seed_presets_once(self, now: int, presets: list) -> int:
        """Insert preset bookmarks (source='seed') exactly once.

        Returns count inserted (0 on every call after the first, even if
        some seeded rows were deleted).
        """
        with self._lock:  # atomic check-then-seed (RLock; inner calls re-enter)
            if self.get_setting("presets_seeded"):
                return 0
            count = 0
            for p in presets:
                self.add_bookmark(
                    now,
                    p["freq_hz"],
                    p.get("demod"),
                    p["label"],
                    notes=p.get("notes", ""),
                    tags=p.get("tags", ()),
                    source="seed",
                )
                count += 1
            self.set_setting("presets_seeded", True)
            return count


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_store: Store | None = None
_store_lock = threading.Lock()


def get_store() -> Store:
    """Return the cached module-level Store, creating it on first call.

    DB location priority:
      1. $AETHERSCOPE_DATA_DIR   (explicit override)
      2. $XDG_DATA_HOME/aetherscope
      3. ~/.local/share/aetherscope  (XDG default)
    """
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        override = os.environ.get("AETHERSCOPE_DATA_DIR")
        if override:
            data_dir = Path(override)
        else:
            xdg = os.environ.get("XDG_DATA_HOME")
            if xdg:
                data_dir = Path(xdg) / "aetherscope"
            else:
                data_dir = Path.home() / ".local" / "share" / "aetherscope"
        data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        _store = Store(data_dir / "aetherscope.db")
    return _store
