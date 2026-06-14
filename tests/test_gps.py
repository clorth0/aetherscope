"""Tests for the gpsd client parsing/state (backend.gps). Hardware-free."""

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.gps import (  # noqa: E402
    GpsClient,
    split_json_lines,
    sky_fields,
    tpv_fields,
)


def test_tpv_3d_fix():
    f = tpv_fields({"class": "TPV", "mode": 3, "lat": 38.72, "lon": -77.81,
                    "altMSL": 177.8, "alt": 177.8, "time": "2026-06-14T18:31:33.000Z"})
    assert f["mode"] == 3
    assert f["lat"] == 38.72 and f["lon"] == -77.81
    assert f["alt"] == 177.8
    assert f["gps_time"] == "2026-06-14T18:31:33.000Z"


def test_tpv_alt_fallback_to_alt():
    # No altMSL: fall back to alt.
    f = tpv_fields({"class": "TPV", "mode": 3, "lat": 1.0, "lon": 2.0, "alt": 99.0})
    assert f["alt"] == 99.0


def test_tpv_no_fix_clears_position():
    f = tpv_fields({"class": "TPV", "mode": 1, "lat": 5.0, "lon": 6.0})
    assert f["mode"] == 1
    assert f["lat"] is None and f["lon"] is None and f["alt"] is None


def test_sky_fields():
    f = sky_fields({"class": "SKY", "uSat": 7, "hdop": 1.2, "vdop": 4.1})
    assert f["sats"] == 7
    assert f["hdop"] == 1.2


def test_split_json_lines_skips_malformed_and_keeps_remainder():
    buf = b'{"class":"SKY","uSat":7}\nnot json\n{"class":"TPV","mode":3,"lat":1,"lon":2}\n{"partial":'
    objs, rem = split_json_lines(buf)
    classes = [o.get("class") for o in objs]
    assert classes == ["SKY", "TPV"]            # malformed line skipped
    assert rem == b'{"partial":'                # incomplete trailing line retained


def test_apply_merges_tpv_then_sky():
    c = GpsClient(enabled=True)
    c.apply({"class": "TPV", "mode": 3, "lat": 38.7, "lon": -77.8, "altMSL": 170.0,
             "time": "2026-06-14T18:31:33.000Z"})
    c.apply({"class": "SKY", "uSat": 7, "hdop": 1.2})
    p = c.position()
    assert p["mode"] == 3
    assert p["lat"] == 38.7 and p["lon"] == -77.8
    assert p["sats"] == 7 and p["hdop"] == 1.2
    assert p["enabled"] is True


def test_position_disabled_has_no_coordinates():
    c = GpsClient(enabled=False)
    c.apply({"class": "TPV", "mode": 3, "lat": 38.7, "lon": -77.8})  # even if data arrived
    p = c.position()
    assert p["enabled"] is False
    assert p["lat"] is None and p["lon"] is None


def test_position_marks_stale_after_timeout():
    c = GpsClient(enabled=True, stale_after_s=5.0)
    c.apply({"class": "TPV", "mode": 3, "lat": 1.0, "lon": 2.0})
    # Fresh now -> not stale; far-future now -> stale.
    assert c.position(now=c._updated_at + 1)["stale"] is False
    assert c.position(now=c._updated_at + 99)["stale"] is True


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
