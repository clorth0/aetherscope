"""Tests for backend.store: SQLite data-access module (TDD)."""

import os
import sys
import tempfile
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.store as store_mod  # noqa: E402
from backend.store import Store, get_store, normalize_tags, validate_bookmark  # noqa: E402


def _make_store():
    """Return a fresh Store on a temp path."""
    d = tempfile.mkdtemp()
    return Store(os.path.join(d, "test.db"))


# ---------------------------------------------------------------------------
# 1. Schema idempotency
# ---------------------------------------------------------------------------

def test_schema_is_idempotent():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "test.db")
    s1 = Store(path)
    s2 = Store(path)  # second open on same path must not error or duplicate tables
    assert s2.list_bookmarks() == []


# ---------------------------------------------------------------------------
# 2. Add + list bookmark
# ---------------------------------------------------------------------------

def test_add_and_list_bookmark():
    s = _make_store()
    now = 1_700_000_000
    b = s.add_bookmark(
        now, 162_550_000, "nfm", "NOAA 1",
        notes="wx satellite", tags=["noaa", "weather"], source="user",
    )
    assert b["freq_hz"] == 162_550_000
    assert b["demod"] == "nfm"
    assert b["label"] == "NOAA 1"
    assert b["notes"] == "wx satellite"
    assert b["tags"] == ["noaa", "weather"]
    assert b["source"] == "user"
    assert b["hit_count"] == 0
    assert b["created_at"] == now
    assert b["updated_at"] == now
    assert b["last_heard_at"] is None
    assert isinstance(b["id"], int)

    rows = s.list_bookmarks()
    assert len(rows) == 1
    assert rows[0]["id"] == b["id"]
    assert rows[0]["tags"] == ["noaa", "weather"]


# ---------------------------------------------------------------------------
# 3. Update bookmark
# ---------------------------------------------------------------------------

def test_update_bookmark():
    s = _make_store()
    now1 = 1_700_000_000
    b = s.add_bookmark(now1, 162_550_000, "nfm", "NOAA 1")

    now2 = now1 + 60
    updated = s.update_bookmark(now2, b["id"], label="NOAA Weather")
    assert updated["label"] == "NOAA Weather"
    assert updated["freq_hz"] == 162_550_000   # unchanged
    assert updated["updated_at"] == now2
    assert updated["created_at"] == now1       # unchanged

    # Unknown id -> None
    result = s.update_bookmark(now2, 99999, label="Ghost")
    assert result is None


def test_update_bookmark_revalidates():
    s = _make_store()
    now = 1_700_000_000
    b = s.add_bookmark(now, 162_550_000, "nfm", "NOAA 1")

    for bad in ({"freq_hz": 0}, {"label": "x" * 81}, {"demod": "ssb"}):
        try:
            s.update_bookmark(now + 1, b["id"], **bad)
            raise AssertionError(f"update_bookmark accepted invalid {bad}")
        except ValueError:
            pass
    # The row is unchanged after the rejected updates
    assert s.list_bookmarks()[0]["label"] == "NOAA 1"


# ---------------------------------------------------------------------------
# 4. Delete bookmark
# ---------------------------------------------------------------------------

def test_delete_bookmark():
    s = _make_store()
    b = s.add_bookmark(1_700_000_000, 100_000_000, "fm", "Test FM")
    assert s.delete_bookmark(b["id"]) is True
    assert s.delete_bookmark(b["id"]) is False
    assert s.list_bookmarks() == []


# ---------------------------------------------------------------------------
# 5. Bump bookmark
# ---------------------------------------------------------------------------

def test_bump_bookmark():
    s = _make_store()
    now = 1_700_000_000
    b = s.add_bookmark(now, 162_550_000, "nfm", "NOAA 1")
    assert b["hit_count"] == 0
    assert b["last_heard_at"] is None

    bumped = s.bump_bookmark(now + 10, b["id"])
    assert bumped["hit_count"] == 1
    assert bumped["last_heard_at"] == now + 10

    bumped2 = s.bump_bookmark(now + 20, b["id"])
    assert bumped2["hit_count"] == 2

    # Unknown id -> None
    assert s.bump_bookmark(now, 99999) is None


# ---------------------------------------------------------------------------
# 6. Tag normalization
# ---------------------------------------------------------------------------

def test_tag_normalization():
    # De-dup, lowercase, strip, drop non-[a-z0-9-] chars
    assert normalize_tags(["AIR", "air", " ATC ", "b@d!"]) == "air,atc,bd"

    # 12 tags capped to 10
    many = [f"tag{i}" for i in range(12)]
    result = normalize_tags(many)
    assert len(result.split(",")) == 10

    # 30-char tag truncated to 24
    long_tag = "a" * 30
    assert normalize_tags([long_tag]) == "a" * 24

    # Comma-string input
    assert normalize_tags("foo, bar, foo") == "foo,bar"

    # Empty inputs
    assert normalize_tags([]) == ""
    assert normalize_tags("") == ""


# ---------------------------------------------------------------------------
# 7. Validation rejects bad input
# ---------------------------------------------------------------------------

def test_validation_rejects_bad_input():
    cases = [
        {"freq_hz": 500_000,       "demod": "fm",  "label": "ok", "notes": ""},          # freq too low
        {"freq_hz": 7_000_000_000, "demod": "fm",  "label": "ok", "notes": ""},          # freq too high
        {"freq_hz": 100_000_000,   "demod": "ssb", "label": "ok", "notes": ""},          # bad demod
        {"freq_hz": 100_000_000,   "demod": "fm",  "label": "",   "notes": ""},          # empty label
        {"freq_hz": 100_000_000,   "demod": "fm",  "label": "x" * 81, "notes": ""},     # label too long
        {"freq_hz": 100_000_000,   "demod": "fm",  "label": "ok", "notes": "n" * 501},  # notes too long
    ]
    for c in cases:
        raised = False
        try:
            validate_bookmark(**c)
        except ValueError:
            raised = True
        assert raised, f"Expected ValueError for {c}"


# ---------------------------------------------------------------------------
# 8. Settings round-trip
# ---------------------------------------------------------------------------

def test_settings_roundtrip():
    s = _make_store()
    s.set_setting("volume", 80)
    assert s.get_setting("volume") == 80

    s.set_setting("config", {"gain": 40, "ppm": 0})
    assert s.get_setting("config") == {"gain": 40, "ppm": 0}

    s.set_setting("name", "aetherscope")
    assert s.get_setting("name") == "aetherscope"

    # Missing key returns default
    assert s.get_setting("missing") is None
    assert s.get_setting("missing", 42) == 42

    # all_settings returns full map
    all_s = s.all_settings()
    assert all_s["volume"] == 80
    assert all_s["name"] == "aetherscope"
    assert "missing" not in all_s

    # Overwrite
    s.set_setting("volume", 50)
    assert s.get_setting("volume") == 50


# ---------------------------------------------------------------------------
# 9. Capture annotations
# ---------------------------------------------------------------------------

def test_capture_annotation():
    s = _make_store()
    fname = "2024-01-01T00:00:00_162550000.iq"

    # Absent -> zero dict with the given filename
    zero = s.get_capture_annotation(fname)
    assert zero["filename"] == fname
    assert zero["user_label"] == ""
    assert zero["notes"] == ""
    assert zero["tags"] == []
    assert zero["last_played_at"] is None

    # Upsert creates row
    ann = s.upsert_capture_annotation(fname, user_label="NOAA pass", notes="clear sky", tags=["noaa"])
    assert ann["filename"] == fname
    assert ann["user_label"] == "NOAA pass"
    assert ann["tags"] == ["noaa"]

    # get returns same
    got = s.get_capture_annotation(fname)
    assert got["user_label"] == "NOAA pass"

    # Second upsert updates fields (tags cleared to empty)
    ann2 = s.upsert_capture_annotation(fname, user_label="NOAA pass updated", notes="updated")
    assert ann2["user_label"] == "NOAA pass updated"
    assert ann2["notes"] == "updated"
    assert ann2["tags"] == []

    # touch_capture sets last_played_at on existing row
    now = 1_700_000_000
    s.touch_capture(now, fname)
    assert s.get_capture_annotation(fname)["last_played_at"] == now

    # touch_capture on absent filename creates a row
    other = "other.iq"
    s.touch_capture(now, other)
    assert s.get_capture_annotation(other)["last_played_at"] == now

    # delete
    assert s.delete_capture_annotation(fname) is True
    assert s.delete_capture_annotation(fname) is False


# ---------------------------------------------------------------------------
# 10. Seed presets once
# ---------------------------------------------------------------------------

def test_seed_presets_once():
    s = _make_store()
    now = 1_700_000_000
    presets = [
        {"freq_hz": 162_550_000, "demod": "nfm", "label": "NOAA 1"},
        {"freq_hz": 162_400_000, "demod": "nfm", "label": "NOAA 2"},
        {"freq_hz": 121_500_000, "demod": "am",  "label": "Guard"},
    ]

    # First call inserts all, returns count
    count = s.seed_presets_once(now, presets)
    assert count == 3

    rows = s.list_bookmarks()
    assert len(rows) == 3
    for r in rows:
        assert r["source"] == "seed"

    # Delete one seeded bookmark
    s.delete_bookmark(rows[0]["id"])
    assert len(s.list_bookmarks()) == 2

    # Second call: returns 0, no re-seeding
    count2 = s.seed_presets_once(now, presets)
    assert count2 == 0
    assert len(s.list_bookmarks()) == 2  # still 2, not restored


# ---------------------------------------------------------------------------
# 11. Parameterized queries (SQL-injection safety)
# ---------------------------------------------------------------------------

def test_parameterized_queries_are_safe():
    s = _make_store()
    now = 1_700_000_000
    evil_label = "'); DROP TABLE bookmarks;--"
    b = s.add_bookmark(now, 100_000_000, "fm", evil_label)

    # Table still intact, label reads back verbatim
    rows = s.list_bookmarks()
    assert len(rows) == 1
    assert rows[0]["label"] == evil_label


# ---------------------------------------------------------------------------
# 12. get_store singleton + AETHERSCOPE_DATA_DIR override
# ---------------------------------------------------------------------------

def test_get_store_singleton():
    d = tempfile.mkdtemp()
    old_env = os.environ.get("AETHERSCOPE_DATA_DIR")
    old_singleton = store_mod._store
    store_mod._store = None                       # isolate from any cached instance
    os.environ["AETHERSCOPE_DATA_DIR"] = d
    try:
        s1 = get_store()
        s2 = get_store()
        assert s1 is s2                           # cached singleton
        assert os.path.exists(os.path.join(d, "aetherscope.db"))
        s1.set_setting("probe", 1)
        assert get_store().get_setting("probe") == 1
    finally:
        store_mod._store = old_singleton
        if old_env is None:
            os.environ.pop("AETHERSCOPE_DATA_DIR", None)
        else:
            os.environ["AETHERSCOPE_DATA_DIR"] = old_env


# ---------------------------------------------------------------------------
# 13. Thread-safety: concurrent inserts do not lose rows
# ---------------------------------------------------------------------------

def test_concurrent_inserts():
    s = _make_store()
    now = 1_700_000_000

    def worker(n):
        for i in range(10):
            s.add_bookmark(now, 100_000_000 + n * 100 + i, "fm", f"t{n}-{i}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(s.list_bookmarks()) == 100  # 10 threads x 10 inserts, none lost


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
