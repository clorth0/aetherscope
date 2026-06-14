# Phase C (part 2): Wireless Device Inventory Design

**Status:** building on `feat/device-inventory`.

## Goal

A persistent "what's around me" catalog: aggregate the contacts with stable
identity (ADS-B aircraft, rtl_433 ISM devices) into the SQLite data layer with
first/last seen, sighting count, type info, and the receiver location, surfaced
in a new Inventory view. Builds on the data layer; complements (does not
duplicate) quietroom's targeted TSCM scoring.

## Sources (v1)

- **ADS-B aircraft** keyed by ICAO hex (`adsb:<hex>`).
- **rtl_433 ISM devices** keyed by model + id (`ism:<model>:<id>`).

Sweep peaks / scanner hits lack persistent identity and are excluded.

## Storage: new `contacts` table in `backend/store.py`

```sql
CREATE TABLE contacts (
  key TEXT PRIMARY KEY,        -- 'adsb:<hex>' | 'ism:<model>:<id>'
  kind TEXT NOT NULL,          -- 'adsb' | 'ism'
  label TEXT NOT NULL,         -- callsign/registration, or sensor model
  ident TEXT NOT NULL DEFAULT '',
  info TEXT NOT NULL DEFAULT '',  -- JSON: reg/country/type or channel/battery/...
  count INTEGER NOT NULL DEFAULT 1,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  lat REAL,
  lon REAL
);
```

Methods (parameterized, thread-safe, no internal wall-clock):
- `record_contact(now, key, kind, label, ident="", info=None, lat=None, lon=None) -> dict`
  — upsert. First sighting inserts with `first_seen=last_seen=now`, `count=1`.
  Later sightings set `last_seen=now`, `count=count+1`, and refresh
  label/ident/info/lat/lon (latest wins). `info` is a dict, JSON-encoded.
- `list_contacts() -> list[dict]` — newest `last_seen` first; `info` decoded to a
  dict.
- `clear_contacts() -> int` — delete all, return rows removed.

## App wiring (`backend/app.py`)

- A small write throttle: `record_contact` is called at most once per ~2 s per
  key (an in-memory `{key: last_write_ts}` guard) so a busy ADS-B feed does not
  hammer SQLite. First sighting of a key always writes.
- `_emit_adsb`: per aircraft, `record_contact("adsb:"+hex, "adsb", label=reg or
  callsign or hex, ident=hex, info={registration,country,type,alt,gs}, lat, lon)`
  using the aircraft's own position.
- The `decoded` event path (`_emit_decoded`, used by live Decode and
  decode-from-file): `record_contact("ism:"+model+":"+id, "ism", label=model,
  ident=id, info={channel,battery,...}, lat/lon=receiver geotag)`.
- `clear_inventory` socket handler -> `clear_contacts`, re-emit.
- `list_inventory` socket handler + `contacts` in the connect snapshot. After a
  batch of records, emit a throttled `contacts` event so the Inventory view
  refreshes.

## Front end

- New **Inventory** mode tab + `#view-inventory`: a searchable, sortable table —
  kind badge, label, ident, info summary, count, first/last seen (relative),
  location. A **Clear inventory** button (with confirm). All contact-derived text
  escaped with `escapeHtml`.
- Populated from the connect `contacts` snapshot and the `contacts` event.

## Privacy

Location is the opt-in receiver geotag (GPS off by default; precision coarsening
applies via the same `gps_precision` path for ISM). The inventory lives in the
local gitignored SQLite. A Clear action wipes it.

## Testing

- `tests/test_store.py` additions: `record_contact` inserts then upserts
  (count increments, last_seen advances, fields refresh, first_seen preserved);
  `list_contacts` ordering + info decode; `clear_contacts`.
- Live: run ADS-B, confirm aircraft populate the inventory with counts + last
  seen; confirm the ISM path records a synthetic decoded event correctly
  (local 433/915 is quiet, so a crafted event verifies the wiring).

## Out of scope

Per-sighting location history (we keep the latest location only); contact
expiry/aging; export (a quick follow-on).
