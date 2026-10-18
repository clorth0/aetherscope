"""Contact-sighting module: characterization + one-module property.

Inventory pinned here (origin/main a673e07, pre-refactor):
  app.py:202-252  live ISM  ism:{model}:{id|channel} -> store.record_contact,
                  per-key 2.0s write throttle (anchored at last WRITE,
                  suppressed attempts do not refresh it), 3.0s contacts emit.
  app.py:370-387  live ADS-B adsb:{hex} -> store.record_contact (same throttle).
  scan.py:249-282 scan rtl_433 {model}/{id|hex|channel} -> phase findings only,
                  dedup for the whole phase (no TTL), first wins, count++,
                  scan_decoded live-emitted for EVERY event. Never the store.
  scan.py:291-314 scan ADS-B hex alone -> phase findings only, latest wins.
Destination is unchanged by the refactor: scan sightings stay scan-only
(the parked sightings->inventory question is NOT answered here).
"""
import os, sys, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest import mock
from backend.contacts import (ContactSighting, ScanSightings, SightingThrottle,
    adsb_inventory_key, adsb_sighting, ism_inventory_key, ism_sighting,
    scan_adsb_key, scan_ism_key)

# --- key byte parity (persisted / frontend-crossing strings) ---
def test_ism_inventory_key_bytes():
    assert ism_inventory_key("Acurite-5n1", "1234") == "ism:Acurite-5n1:1234"
    assert ism_sighting({"model": "Acurite-5n1", "id": 1234}).key == "ism:Acurite-5n1:1234"

def test_adsb_inventory_key_bytes():
    assert adsb_inventory_key("abc123") == "adsb:abc123"
    s = adsb_sighting({"hex": "abc123"}, registration=None, country=None)
    assert s.key == "adsb:abc123" and s.kind == "adsb" and s.ident == "abc123"

def test_scan_key_bytes():
    assert scan_ism_key({"model": "M", "id": 7}) == "M/7"
    assert scan_ism_key({"model": "M", "hex": "ff"}) == "M/ff"          # hex fallback
    assert scan_ism_key({"model": "M", "channel": 2}) == "M/2"          # channel fallback
    assert scan_ism_key({"channel": 2}) == "?/2"                        # model default '?'
    assert scan_ism_key({"model": "M"}) == "M/?"                        # ident default '?'
    assert scan_ism_key({"model": "M", "id": 7, "hex": "ff"}) == "M/7"  # id wins over hex
    assert scan_adsb_key("abc123") == "abc123" and scan_adsb_key("") is None

# --- sighting derivation parity ---
def test_ism_sighting_derivation():
    ev = {"model": "M", "channel": 3, "battery_ok": 1, "temperature_C": 20.5,
          "humidity": 40, "freq": 433.9, "rssi": -50, "other": "dropped"}
    s = ism_sighting(ev, lat=1.0, lon=2.0)
    assert isinstance(s, ContactSighting)
    assert (s.kind, s.label, s.ident) == ("ism", "M", "3")  # channel fallback, str()
    assert s.info == {"channel": 3, "battery_ok": 1, "temperature_C": 20.5,
                      "humidity": 40, "freq": 433.9, "rssi": -50}
    assert (s.lat, s.lon) == (1.0, 2.0)
    assert ism_sighting({"model": "M", "id": 9, "channel": 3}).ident == "9"  # id wins
    assert ism_sighting({"id": 9}) is None                                    # no model
    assert ism_sighting({"model": "M"}).ident == ""                           # neither

def test_adsb_sighting_derivation():
    a = {"hex": "abc123", "flight": " UAL1 ", "alt_baro": 10000, "gs": 250.0, "lat": 1.5, "lon": -2.5}
    s = adsb_sighting(a, registration="N123", country="United States")
    assert (s.label, s.ident) == ("UAL1", "abc123")
    assert s.info == {"registration": "N123", "country": "United States", "alt": 10000, "gs": 250.0}
    assert (s.lat, s.lon) == (1.5, -2.5)
    assert adsb_sighting({"hex": "abc123"}, registration="N123", country=None).label == "N123"
    assert adsb_sighting({"hex": "abc123"}, registration=None, country=None).label == "ABC123"
    assert adsb_sighting({"hex": "abc123"}, registration=None, country=None).info == {}
    assert adsb_sighting({"flight": "X"}, registration=None, country=None) is None  # no hex

# --- throttle: app write dedup (2.0s, anchored at last write) ---
def test_throttle_window():
    t = SightingThrottle(2.0)
    assert t.register("ism:M:1", 100.0) is True    # first passes
    assert t.register("ism:M:1", 101.9) is False   # within window suppressed
    assert t.register("ism:M:1", 101.99) is False  # suppressed attempt does NOT refresh
    assert t.register("ism:M:1", 102.0) is True    # at window passes (was < 2.0)
    assert t.register("ism:M:1", 102.5) is False
    assert t.register("ism:Other:1", 102.5) is True   # different model not suppressed
    assert t.register("ism:M:2", 102.5) is True       # different ident not suppressed
    t.clear(); assert t.register("ism:M:1", 102.6) is True  # clear_inventory parity

# --- scan dedup: phase-lifetime, no TTL ---
def test_scan_ism_dedup():
    c = ScanSightings()
    e1 = {"model": "M", "id": 7, "time": "t1", "temperature_C": 1}
    assert c.add_ism(e1) == "M/7"
    assert c.add_ism({"model": "M", "id": 7, "time": "t2"}) == "M/7"
    assert c.add_ism({"model": "Other", "id": 7, "time": "t3"}) == "Other/7"  # model separates
    assert c.add_ism({"model": "M", "id": 8, "time": "t4"}) == "M/8"
    devs = c.ism_devices()
    assert [d["_key"] for d in devs] == ["M/7", "Other/7", "M/8"]
    assert devs[0]["_count"] == 2 and devs[0]["_last_time"] == "t2"
    assert devs[0]["temperature_C"] == 1  # first event wins

def test_scan_adsb_hex_alone_collision():
    c = ScanSightings()
    assert c.add_adsb({"hex": "abc", "flight": "A"}) == "abc"
    # hex alone: same hex replaces, even though flight differs (today's behaviour)
    assert c.add_adsb({"hex": "abc", "flight": "B"}) == "abc"
    assert c.add_adsb({"flight": "nohex"}) is None
    lst = c.adsb_aircraft()
    assert len(lst) == 1 and lst[0]["flight"] == "B"  # latest wins

# --- consumers: app + scan both derive through the one module ---
def test_app_emit_event_records_through_module():
    from backend import app as appmod
    recorded, emitted = [], []
    class FakeStore:
        def record_contact(self, now, key, kind, label, ident="", info=None, lat=None, lon=None):
            recorded.append((key, kind, label, ident, info))
        def list_contacts(self): return []
    with mock.patch.object(appmod, "get_store", lambda: FakeStore()), \
         mock.patch.object(appmod.socketio, "emit", lambda n, p=None: emitted.append((n, p))), \
         mock.patch.object(appmod, "_receiver_geotag", lambda: None):
        appmod._contact_throttle.clear()
        ev = {"model": "M", "id": 7, "temperature_C": 5}
        appmod._emit_event(dict(ev)); appmod._emit_event(dict(ev))  # repeat suppressed by throttle
        assert [e[0] for e in emitted] == ["decoded", "decoded"]    # socket event every time
    assert recorded == [("ism:M:7", "ism", "M", "7", {"temperature_C": 5})]

def test_app_emit_adsb_records_through_module():
    from backend import app as appmod
    recorded = []
    class FakeStore:
        def record_contact(self, now, key, kind, label, ident="", info=None, lat=None, lon=None):
            recorded.append((key, kind, label, ident, info, lat, lon))
        def list_contacts(self): return []
    with mock.patch.object(appmod, "get_store", lambda: FakeStore()), \
         mock.patch.object(appmod.socketio, "emit", lambda n, p=None: None), \
         mock.patch.object(appmod, "_contact_throttle", SightingThrottle(0.0)):
        a = {"hex": "abc123", "flight": "UAL1", "lat": 1.0, "lon": 2.0}
        appmod._emit_adsb([dict(a), {"flight": "nohex"}], {})
    assert len(recorded) == 1
    key, kind, label, ident, info, lat, lon = recorded[0]
    assert (key, kind, label, ident, lat, lon) == ("adsb:abc123", "adsb", "UAL1", "abc123", 1.0, 2.0)

def test_scan_phases_consume_module_and_stay_scan_only():
    import backend.scan as scanmod
    from backend.scan import AutoScanner, ScanConfig
    evs = [{"model": "M", "id": 7, "time": "t1"}, {"model": "M", "id": 7, "time": "t2"},
           {"model": "Other", "id": 7, "time": "t3"}]
    acs = [{"hex": "abc", "flight": "A"}, {"hex": "abc", "flight": "B"}, {"flight": "nohex"}]
    class FakeDecoder:
        def __init__(self, cfg, on_event): self.on_event = on_event
        def start(self):
            for e in evs: self.on_event(e)
        def stop(self): pass
    class FakeRecv:
        def __init__(self, cfg, on_update): self.on_update = on_update
        def start(self): self.on_update(acs, {})
        def stop(self): pass
    emitted = []
    sc = AutoScanner(ScanConfig(rtl433_seconds=0.01, adsb_seconds=0.01),
                     on_event=lambda n, p: emitted.append((n, p)))
    with mock.patch.object(scanmod, "Rtl433Decoder", FakeDecoder), \
         mock.patch.object(scanmod, "AdsbReceiver", FakeRecv):
        devices = sc._phase_rtl433(433_920_000, "ISM 433 MHz", "rtl433_433")
        aircraft = sc._phase_adsb()
    assert [d["_key"] for d in devices] == ["M/7", "Other/7"]
    assert devices[0]["_count"] == 2 and devices[0]["_last_time"] == "t2"
    assert len([e for e in emitted if e[0] == "scan_decoded"]) == 3  # live emit not deduped
    assert sc._phases[0].findings["devices"] is devices              # findings, not store
    assert len(aircraft) == 1 and aircraft[0]["flight"] == "B" and sc._phases[1].findings["aircraft"] is aircraft
    # scan path has no store reference at all (destination unchanged)
    assert not hasattr(scanmod, "get_store") and not hasattr(scanmod, "record_contact")

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try: t(); print(f"PASS {t.__name__}")
        except Exception: failed += 1; print(f"FAIL {t.__name__}"); traceback.print_exc()
    print(f"\n{len(tests)-failed}/{len(tests)} passed"); raise SystemExit(1 if failed else 0)
