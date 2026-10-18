"""Contact-sighting module: one home for sighting identity and dedup.

Deep module (see docs/architecture.md). A decoded rtl_433 event or an
ADS-B aircraft dict becomes a sighting in exactly one place; app.py
(live) and scan.py (Auto-Scan) used to re-derive that independently:
app built ``ism:{model}:{ident}`` / ``adsb:{hex}`` for the persistent
inventory while scan deduped on ``{model}/{id|hex|channel}`` and on hex
alone, in private dicts per phase.

Two keys, named explicitly because they are genuinely different:

  inventory_key  durable identity, persisted as the contacts table's
                 PRIMARY KEY in sqlite and crossed to the frontend in
                 ``contacts`` events: ``ism:{model}:{ident}`` and
                 ``adsb:{hex}``. Byte-identical to the pre-refactor
                 strings -- renaming one would orphan stored history.
  scan_key       ephemeral identity, lives only inside one scan phase's
                 findings (``_key`` on a device dict): ``{model}/``
                 ``{id|hex|channel}`` for rtl_433, the hex alone for
                 ADS-B. Never persisted, never crossed to the store.

Dedup likewise has two real semantics, both owned here:

  SightingThrottle  time-window dedup for durable writes. A repeat of
                    the same inventory_key within ``window_s`` is
                    suppressed; the window is anchored at the last
                    *passed* sighting (a suppressed attempt does not
                    refresh it). At/after the window the sighting passes.
  ScanSightings     phase-lifetime dedup for a scan. No TTL: a key seen
                    at any point in the phase stays seen until the phase
                    ends. rtl_433 keeps the first event and counts
                    repeats; ADS-B keeps the latest aircraft per hex.

Destination is NOT decided here. This module says what a sighting *is*
and whether it is a repeat; callers keep deciding where it goes. Today
live sightings go to the store (via app.py) and scan sightings go to
scan findings only -- whether scan sightings should also enter the
persistent inventory is a parked product question this module
deliberately does not answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Info fields a live ISM sighting carries into the inventory, in order.
_ISM_INFO_FIELDS = ("channel", "battery_ok", "temperature_C", "humidity", "freq", "rssi")


@dataclass(frozen=True)
class ContactSighting:
    """One sighting, ready for a caller to route wherever it routes it."""

    key: str            # inventory_key (see module docstring)
    kind: str           # "ism" | "adsb"
    label: str
    ident: str = ""
    info: dict = field(default_factory=dict)
    lat: float | None = None
    lon: float | None = None


# ------------------------------------------------------------------
# Inventory keys (durable; bytes are persisted -- do not rename)
# ------------------------------------------------------------------

def ism_inventory_key(model, ident) -> str:
    return f"ism:{model}:{ident}"


def adsb_inventory_key(hex_id) -> str:
    return f"adsb:{hex_id}"


# ------------------------------------------------------------------
# Scan keys (ephemeral; one scan phase only)
# ------------------------------------------------------------------

def scan_ism_key(event: dict) -> str:
    return f"{event.get('model', '?')}/{event.get('id', event.get('hex', event.get('channel', '?')))}"


def scan_adsb_key(hex_id) -> str | None:
    return hex_id if hex_id else None


# ------------------------------------------------------------------
# Sighting derivation
# ------------------------------------------------------------------

def ism_sighting(event: dict, lat=None, lon=None) -> ContactSighting | None:
    """A live rtl_433 event as a sighting, or None when it has no model."""
    model = event.get("model")
    if not model:
        return None
    ident = str(event.get("id", event.get("channel", "")))
    info = {k: event[k] for k in _ISM_INFO_FIELDS if k in event}
    return ContactSighting(
        key=ism_inventory_key(model, ident), kind="ism", label=str(model),
        ident=ident, info=info, lat=lat, lon=lon)


def adsb_sighting(aircraft: dict, registration=None, country=None) -> ContactSighting | None:
    """A live ADS-B aircraft dict as a sighting, or None when it has no hex.

    ``registration`` / ``country`` are the caller's enrichment (readsb's
    own ``r`` field preferred, else the adsb_enrich lookup); this module
    owns only how they join the label/info, not where they come from.
    """
    hx = aircraft.get("hex")
    if not hx:
        return None
    label = (aircraft.get("flight") or "").strip() or registration or hx.upper()
    info = {k: v for k, v in {"registration": registration, "country": country,
                              "alt": aircraft.get("alt_baro"),
                              "gs": aircraft.get("gs")}.items()
            if v is not None}
    return ContactSighting(
        key=adsb_inventory_key(hx), kind="adsb", label=label, ident=hx,
        info=info, lat=aircraft.get("lat"), lon=aircraft.get("lon"))


# ------------------------------------------------------------------
# Dedup
# ------------------------------------------------------------------

class SightingThrottle:
    """Window dedup behind the live inventory writes (see module docstring)."""

    def __init__(self, window_s: float):
        self._window_s = window_s
        self._last_passed: dict[str, float] = {}

    def register(self, key: str, now: float) -> bool:
        """True when this sighting passes (first, or at/after the window).

        A suppressed repeat returns False and does not move the window.
        """
        if now - self._last_passed.get(key, 0.0) < self._window_s:
            return False
        self._last_passed[key] = now
        return True

    def clear(self) -> None:
        self._last_passed.clear()


class ScanSightings:
    """Phase-lifetime dedup behind one scan's findings (see module docstring)."""

    def __init__(self):
        self._ism: dict[str, dict] = {}   # scan_key -> {first, count, last_time}
        self._adsb: dict[str, dict] = {}  # hex -> latest aircraft dict

    def add_ism(self, event: dict) -> str:
        """Register one rtl_433 event; returns its scan_key.

        First event for a key wins; repeats bump count/last_time only.
        """
        key = scan_ism_key(event)
        if key not in self._ism:
            self._ism[key] = {"first": event, "count": 0, "last_time": event.get("time")}
        self._ism[key]["count"] += 1
        self._ism[key]["last_time"] = event.get("time")
        return key

    def ism_devices(self) -> list[dict]:
        """The deduped device list: first event + _count/_last_time/_key."""
        devices: list[dict] = []
        for key, info in self._ism.items():
            d = dict(info["first"])
            d["_count"] = info["count"]
            d["_last_time"] = info["last_time"]
            d["_key"] = key
            devices.append(d)
        return devices

    def add_adsb(self, aircraft: dict) -> str | None:
        """Register one aircraft dict; returns its hex, or None without one.

        Keyed on hex alone: a later dict with the same hex replaces the
        earlier one (latest wins), whatever else differs.
        """
        key = scan_adsb_key(aircraft.get("hex"))
        if key is None:
            return None
        self._adsb[key] = aircraft
        return key

    def adsb_aircraft(self) -> list[dict]:
        return list(self._adsb.values())
