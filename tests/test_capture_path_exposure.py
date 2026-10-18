"""Capture path exposure (security question #5): wire payloads carry no
absolute filesystem paths, and /captures/ serves only contained files.

Characterization (pre-fix, 2026-10-18):
- /api/captures and the socket `captures` payload included absolute
  `path` and `sidecar` for every capture; `capture_started`/`capture_done`
  records (asdict) carried them too. Frontend reads (grep of
  frontend/static/*.js for `.path`/`.sidecar` and `c.*` fields in
  aether-capture.js): name,label,user_label,notes,tags,freq_hz,kind,
  missing,geolocation,geolocation_redacted,sigmf,sigmf_name,duration_s,
  file_size,demod,audio_rate,sample_rate,sample_format,started_at -- it
  never reads `path` or `sidecar`, building download/play URLs from
  `name`/`sigmf_name` instead. So stripping them breaks nothing.
- GET /captures/<path:name> relied only on send_from_directory: `../`
  and absolute names 404'd, but a symlink inside the captures dir
  pointing outside was served byte-for-byte.
- delete/redact take a *name* (catalogue.capture_basename rejects '/'
  and '..', then builds root/<base><suffix>); update_capture takes a
  filename used only as a parameterized SQLite key. No handler passed a
  raw client path to the filesystem (verified by reading those
  handlers); these tests pin that.
"""

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AETHERSCOPE_DATA_DIR", tempfile.mkdtemp(prefix="aether-paths-"))

from backend import app as appmod  # noqa: E402
from backend import capture as capmod  # noqa: E402
from backend.catalogue import delete_capture, resolve_capture_file, wire_capture_dict  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _seed(tmp: Path):
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "a.iq").write_bytes(b"IQDATA")
    (tmp / "a.sigmf-meta").write_text(json.dumps({"global": {}}))
    (tmp / "a.json").write_text(json.dumps({
        "name": "a.iq", "path": str(tmp / "a.iq"), "sidecar": str(tmp / "a.json"),
        "freq_hz": 433_920_000, "sample_rate": 8_000_000, "duration_s": 1.0,
        "started_at": 100.0, "finished_at": 101.0, "file_size": 6,
        "sample_format": "cs8", "label": "note", "geolocation": None,
    }))
    (tmp / "b.wav").write_bytes(b"WAVDATA")
    (tmp / "b.json").write_text(json.dumps({
        "kind": "audio", "name": "b.wav", "path": str(tmp / "b.wav"),
        "sidecar": str(tmp / "b.json"), "freq_hz": 101_100_000, "demod": "fm",
        "audio_rate": 48_000, "started_at": 200.0, "finished_at": None,
        "file_size": 7, "duration_s": 1.5, "label": "tone", "geolocation": None,
    }))
    return tmp


class _captures_dir:
    def __init__(self, path):
        self.path = Path(path)
    def __enter__(self):
        self._old_app, self._old_cap = appmod.CAPTURES_DIR, capmod.CAPTURES_DIR
        appmod.CAPTURES_DIR = capmod.CAPTURES_DIR = self.path
        return self.path
    def __exit__(self, *exc):
        appmod.CAPTURES_DIR, capmod.CAPTURES_DIR = self._old_app, self._old_cap
        return False


def _assert_no_absolute_paths(payload, captures_dir):
    text = json.dumps(payload)
    assert str(captures_dir) not in text, text
    def walk(v):
        if isinstance(v, dict):
            assert "path" not in v and "sidecar" not in v, v
            for x in v.values(): walk(x)
        elif isinstance(v, list):
            for x in v: walk(x)
        elif isinstance(v, str):
            assert not (v.startswith("/") and Path(v).is_absolute()), v
    walk(payload)


def test_api_captures_strips_absolute_paths_keeps_frontend_fields():
    with _captures_dir(_seed(Path(tempfile.mkdtemp()))) as tmp:
        client = appmod.app.test_client()
        resp = client.get("/api/captures")
        assert resp.status_code == 200
        items = resp.get_json()
        _assert_no_absolute_paths(items, tmp)
        assert [i["name"] for i in items] == ["b.wav", "a.iq"]
        iq = next(i for i in items if i["name"] == "a.iq")
        for field in ("name", "freq_hz", "sample_rate", "duration_s",
                      "started_at", "finished_at", "file_size",
                      "sample_format", "label", "geolocation"):
            assert field in iq, field


def test_socket_captures_payload_strips_paths_keeps_enrichment():
    with _captures_dir(_seed(Path(tempfile.mkdtemp()))) as tmp:
        items = appmod._enriched_captures()
        _assert_no_absolute_paths(items, tmp)
        iq = next(i for i in items if i["name"] == "a.iq")
        # fields the frontend actually reads from the socket list
        for field in ("name", "label", "user_label", "notes", "tags",
                      "freq_hz", "duration_s", "file_size", "sample_rate",
                      "sample_format", "started_at", "kind", "missing",
                      "sigmf", "sigmf_name", "geolocation"):
            assert field in iq, field
        assert iq["sigmf_name"] == "a.sigmf-meta" and iq["sigmf"] is True


def test_capture_record_wire_shape_strips_paths():
    # capture_started/capture_done send wire_capture_dict(asdict(record));
    # the frontend reads only name/label/freq/sample_rate/duration_s there.
    rec = {"name": "a.iq", "path": "/x/a.iq", "sidecar": "/x/a.json",
           "label": "n", "freq_hz": 1, "sample_rate": 2, "duration_s": 1.0}
    assert wire_capture_dict(rec) == {"name": "a.iq", "label": "n",
        "freq_hz": 1, "sample_rate": 2, "duration_s": 1.0}
    app_src = (ROOT / "backend" / "app.py").read_text()
    assert 'wire_capture_dict(asdict(extra[0]))' in app_src  # capture_done
    assert 'wire_capture_dict(asdict(worker.record))' in app_src  # capture_started


def test_frontend_never_reads_path_or_sidecar():
    js = "\n".join(p.read_text() for p in (ROOT / "frontend" / "static").glob("*.js"))
    assert ".path" not in js and ".sidecar" not in js
    assert '"/captures/"' in js  # playback/download are built from name


def test_contained_capture_served_byte_identical():
    with _captures_dir(_seed(Path(tempfile.mkdtemp()))):
        client = appmod.app.test_client()
        for name, want in (("a.iq", b"IQDATA"), ("b.wav", b"WAVDATA")):
            resp = client.get(f"/captures/{name}")
            assert resp.status_code == 200 and resp.data == want, name
        assert resolve_capture_file("a.iq", appmod.CAPTURES_DIR) is not None


def test_traversal_absolute_and_symlink_escapes_rejected():
    with tempfile.TemporaryDirectory() as td:
        tmp, outside = _seed(Path(td) / "caps"), Path(td) / "outside.iq"
        outside.write_bytes(b"SECRET")
        (tmp / "evil.iq").symlink_to(outside)
        with _captures_dir(tmp):
            client = appmod.app.test_client()
            for path in ("/captures/../outside.iq", "/captures/..%2foutside.iq",
                         "/captures/%2e%2e%2foutside.iq", "/captures/..%5coutside.iq",
                         "/captures/%2fetc%2fpasswd", "/captures//etc/passwd",
                         "/captures/evil.iq", "/captures/sub/a.iq",
                         "/captures/a.txt", "/captures/a.json", "/captures/nope.iq"):
                resp = client.get(path)
                assert resp.status_code in (400, 404), (path, resp.status_code)
                assert b"SECRET" not in resp.data and b"root:" not in resp.data, path
            assert resolve_capture_file("../outside.iq", tmp) is None
            assert resolve_capture_file("/etc/passwd", tmp) is None
            assert resolve_capture_file("evil.iq", tmp) is None
            assert resolve_capture_file("a.iq", tmp).read_bytes() == b"IQDATA"


def test_client_supplied_path_into_delete_does_not_touch_files():
    with tempfile.TemporaryDirectory() as td:
        tmp = _seed(Path(td) / "caps")
        outside = Path(td) / "outside.iq"; outside.write_bytes(b"SECRET")
        # a path, an absolute path, and a traversal name are not capture names
        for bad in (str(outside), "../outside.iq", "..%2foutside.iq", "/etc/passwd"):
            assert delete_capture(bad, tmp) is False, bad
        assert outside.read_bytes() == b"SECRET"
        assert (tmp / "a.iq").exists()
        # the legitimate name still deletes its own quartet only
        assert delete_capture("a.iq", tmp) is True
        assert not (tmp / "a.iq").exists() and outside.exists()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"PASS {t.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {t.__name__}"); traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
