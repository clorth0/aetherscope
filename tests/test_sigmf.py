"""Tests for SigMF metadata building (backend.sigmf). Hardware-free."""

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.sigmf import build_sigmf_meta  # noqa: E402


def _record(**over):
    rec = {
        "name": "2026-06-14_100MHz.iq",
        "freq_hz": 100_000_000,
        "sample_rate": 8_000_000,
        "started_at": 1_700_000_000.0,
        "label": "",
        "geolocation": None,
    }
    rec.update(over)
    return rec


def test_build_basic_structure():
    m = build_sigmf_meta(_record())
    assert set(m) == {"global", "captures", "annotations"}
    g = m["global"]
    assert g["core:datatype"] == "ci8"
    assert "core:version" in g
    assert g["core:sample_rate"] == 8_000_000.0
    assert g["core:dataset"] == "2026-06-14_100MHz.iq"   # NCD reference to the .iq
    assert g["core:hw"] == "HackRF One"
    cap = m["captures"][0]
    assert cap["core:sample_start"] == 0
    assert cap["core:frequency"] == 100_000_000.0
    assert m["annotations"] == []
    json.dumps(m)  # must be serializable


def test_geolocation_becomes_geojson_point():
    geo = {"lat": 38.7233, "lon": -77.8134, "alt_m": 179.2}
    g = build_sigmf_meta(_record(geolocation=geo))["global"]
    assert g["core:geolocation"] == {"type": "Point", "coordinates": [-77.8134, 38.7233, 179.2]}


def test_no_geolocation_key_when_absent():
    g = build_sigmf_meta(_record(geolocation=None))["global"]
    assert "core:geolocation" not in g


def test_datetime_is_iso8601_utc():
    cap = build_sigmf_meta(_record())["captures"][0]
    assert cap["core:datetime"].startswith("2023-11-14T22:13:20")
    assert cap["core:datetime"].endswith("Z")


def test_label_becomes_description():
    g = build_sigmf_meta(_record(label="airband"))["global"]
    assert g["core:description"] == "airband"
    g2 = build_sigmf_meta(_record(label=""))["global"]
    assert "core:description" not in g2


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
