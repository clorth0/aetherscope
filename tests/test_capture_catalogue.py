"""Capture catalogue: one typed home for sidecar shape, names, and slugs.

Characterization pins (pre-refactor behaviour, from capture.py /
audio_record.py / app.py / waterfall.js):
- IQ sidecar keys: name,path,sidecar,freq_hz,sample_rate,duration_s,
  started_at,finished_at,file_size,sample_format,label,geolocation
  (+ geolocation_redacted only after redaction). No kind key.
- Audio sidecar keys: kind,name,path,sidecar,freq_hz,demod,audio_rate,
  started_at,finished_at,file_size,duration_s,label,geolocation.
- Start functions defaulted freq_hz to 0 and sample_rate to 2_000_000.
"""

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.catalogue import (  # noqa: E402
    AudioCaptureEntry,
    IqCaptureEntry,
    capture_basename,
    delete_capture,
    find_capture,
    list_catalogue,
    parse_capture,
    redact_location,
    sigmf_meta_name,
    slugify,
)


def _iq(**over):
    d = {
        "name": "2026-01-01_note.iq", "path": "/x/2026-01-01_note.iq",
        "sidecar": "/x/2026-01-01_note.json", "freq_hz": 433_920_000,
        "sample_rate": 8_000_000, "duration_s": 5.0, "started_at": 100.0,
        "finished_at": 105.0, "file_size": 10, "sample_format": "cs8",
        "label": "note", "geolocation": None,
    }
    d.update(over)
    return d


def _wav(**over):
    d = {
        "kind": "audio", "name": "2026-01-01_tone.wav",
        "path": "/x/2026-01-01_tone.wav", "sidecar": "/x/2026-01-01_tone.json",
        "freq_hz": 101_100_000, "demod": "fm", "audio_rate": 50_000,
        "started_at": 200.0, "finished_at": None, "file_size": 20,
        "duration_s": 1.5, "label": "tone", "geolocation": None,
    }
    d.update(over)
    return d


def test_slugify_matches_both_old_copies():
    assert slugify("  My Label!  ") == "My_Label"
    assert slugify("a/b c.d-e") == "a_b_c.d-e"
    assert slugify("") == "capture"
    assert slugify("", fallback="audio") == "audio"
    assert slugify("   ", fallback="audio") == "audio"
    assert slugify(None) == "capture"  # audio copy tolerated None


def test_basename_and_sigmf_name_derivation():
    assert capture_basename("a.b.iq") == "a.b"
    assert capture_basename("a.b.sigmf-meta") == "a.b"
    assert capture_basename("clip.wav") == "clip"
    assert sigmf_meta_name("a.iq") == "a.sigmf-meta"
    assert sigmf_meta_name("a.b.iq") == "a.b.sigmf-meta"  # multi-dot
    assert sigmf_meta_name("a.wav") is None
    assert sigmf_meta_name("a.json") is None
    assert sigmf_meta_name("already-a-stem") is None
    assert sigmf_meta_name(".iq") is None


def test_parse_iq_shape_and_start_defaults():
    e = parse_capture(_iq())
    assert isinstance(e, IqCaptureEntry) and e.kind == "iq"
    assert e.freq_hz == 433_920_000 and e.sample_rate == 8_000_000
    assert e.sigmf_name == "2026-01-01_note.sigmf-meta"
    assert not hasattr(e, "demod")
    bare = parse_capture({"name": "x.iq", "path": "/x/x.iq"})
    assert bare.freq_hz == 0 and bare.sample_rate == 2_000_000  # start-fn defaults
    assert bare.sample_format == "cs8"


def test_parse_audio_shape_is_a_different_shape():
    e = parse_capture(_wav())
    assert isinstance(e, AudioCaptureEntry) and e.kind == "audio"
    assert e.demod == "fm" and e.audio_rate == 50_000
    assert e.sigmf_name is None
    assert not hasattr(e, "sample_rate")


def test_to_dict_round_trips_the_wire_shape_exactly():
    assert parse_capture(_iq()).to_dict() == _iq()
    assert list(parse_capture(_iq()).to_dict()) == list(_iq())
    assert parse_capture(_wav()).to_dict() == _wav()
    assert list(parse_capture(_wav()).to_dict()) == list(_wav())
    redacted = _iq(geolocation_redacted=True)
    assert parse_capture(redacted).to_dict() == redacted


def _seed(tmp):
    d = Path(tmp)
    (d / "a.iq").write_bytes(b"1234567")
    (d / "a.json").write_text(json.dumps(_iq(
        name="a.iq", path=str(d / "a.iq"), sidecar=str(d / "a.json"), file_size=1)))
    (d / "a.sigmf-meta").write_text(json.dumps({"global": {"core:geolocation": {"x": 1}}}))
    (d / "b.wav").write_bytes(b"12")
    (d / "b.json").write_text(json.dumps(_wav(
        name="b.wav", path=str(d / "b.wav"), sidecar=str(d / "b.json"), started_at=300.0)))
    (d / "broken.json").write_text("{nope")
    return d


def test_list_and_find_catalogue():
    d = _seed(tempfile.mkdtemp())
    items = list_catalogue(d)
    assert [e.name for e in items] == ["b.wav", "a.iq"]  # newest first, broken skipped
    iq = find_capture("a.iq", d)
    assert isinstance(iq, IqCaptureEntry) and iq.file_size == 7  # refreshed from disk
    assert iq.exists and not iq.missing_in(d) and iq.sigmf_exists_in(d)
    assert find_capture("nope.iq", d) is None
    assert find_capture("b.wav", d).missing_in(d) is False
    (d / "b.wav").unlink()
    assert find_capture("b.wav", d).missing_in(d) is True


def test_redact_and_delete_quartet():
    d = _seed(tempfile.mkdtemp())
    side = json.loads((d / "a.json").read_text())
    side["geolocation"] = {"lat": 1.0, "lon": 2.0}
    (d / "a.json").write_text(json.dumps(side))
    assert redact_location("a.iq", d) is True
    out = json.loads((d / "a.json").read_text())
    assert out["geolocation"] is None and out["geolocation_redacted"] is True
    assert "core:geolocation" not in json.loads((d / "a.sigmf-meta").read_text())["global"]
    assert delete_capture("a.iq", d) is True
    assert not any((d / n).exists() for n in ("a.iq", "a.json", "a.sigmf-meta"))
    assert delete_capture("../secret.iq", d) is False
    assert redact_location("nested/name.iq", d) is False


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
