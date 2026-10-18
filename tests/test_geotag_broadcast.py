"""Broadcast (live socket) geotag precision must differ from stored precision.

Stored captures keep the operator's gps_precision choice; anything emitted
to connected sockets gets the broadcast precision, coarse (~1 km) by default.
Full broadcast precision is an explicit opt-in only.
"""
import os, sys, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.gps import broadcast_geolocation, broadcast_position, coarsen_geolocation

RAW = {"enabled": True, "connected": True, "mode": 3, "lat": 38.723298, "lon": -77.813422,
       "alt": 179.2, "hdop": 1.2, "sats": 7, "gps_time": "t", "stale": False, "updated_at": 1}

def test_default_broadcast_never_full_precision():
    out = broadcast_position(dict(RAW))
    assert out["lat"] == 38.72 and out["lon"] == -77.81, out
    assert out["lat"] != RAW["lat"] and out["lon"] != RAW["lon"]
    assert out["precision"] == "~1km"
    # raw input not mutated
    assert RAW["lat"] == 38.723298

def test_default_broadcast_geolocation_coarse_and_stored_unchanged():
    geo = {"lat": 38.723298, "lon": -77.813422, "alt_m": 179.2, "source": "gpsd"}
    out = broadcast_geolocation(geo)
    assert out["lat"] == 38.72 and out["lon"] == -77.81 and out["precision"] == "~1km"
    stored = coarsen_geolocation(geo, "full")
    assert stored["lat"] == 38.723298 and stored["lon"] == -77.813422

def test_explicit_full_broadcast_opt_in_emits_full():
    out = broadcast_position(dict(RAW), "full")
    assert out["lat"] == RAW["lat"] and out["lon"] == RAW["lon"] and out["precision"] == "full"

def test_broadcast_100m_opt_in_rounds_to_3_decimals():
    out = broadcast_position(dict(RAW), "100m")
    assert out["lat"] == 38.723 and out["lon"] == -77.813

def test_unknown_broadcast_precision_fails_closed_to_coarse():
    out = broadcast_position(dict(RAW), "bogus")
    assert out["lat"] == 38.72 and out["lon"] == -77.81

def test_broadcast_no_fix_passes_through_without_coords():
    out = broadcast_position({"enabled": True, "lat": None, "lon": None})
    assert out["lat"] is None and out["lon"] is None
    assert broadcast_geolocation(None) is None

def test_app_emission_seam_uses_broadcast_and_allowlist():
    src = open(os.path.join(os.path.dirname(__file__), "..", "backend", "app.py")).read()
    assert '"gps_broadcast_precision"' in src
    # every gps_status emission must go through the broadcast seam, none raw
    assert 'socketio.emit("gps_status", p)' not in src
    assert 'socketio.emit("gps_status", _gps.position())' not in src
    assert 'emit("gps_status", _gps.position())' not in src
    assert src.count("gps_status") >= 3

if __name__ == "__main__":
    tests=[v for k,v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed=0
    for t in tests:
        try: t(); print(f"PASS {t.__name__}")
        except Exception: failed+=1; print(f"FAIL {t.__name__}"); traceback.print_exc()
    print(f"\n{len(tests)-failed}/{len(tests)} passed"); raise SystemExit(1 if failed else 0)
