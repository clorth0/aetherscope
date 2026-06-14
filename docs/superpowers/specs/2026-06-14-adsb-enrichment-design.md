# Phase C (part 1): ADS-B Enrichment Design

**Status:** building on `feat/adsb-enrich`.

## Goal

Make the ADS-B map more useful: show each aircraft's US registration (N-number)
and country, draw range rings around the receiver, and report richer stats
(message rate, max range). First Phase C (situational-awareness) sub-feature.

## Findings

readsb (this build) emits `aircraft.json` without a registration/type database:
fields are `hex`, `alt_baro`, `alt_geom`, `gs`, `track`, `messages`, `rssi`,
`seen`, `flight` (when broadcast), `lat`/`lon` (when position broadcast), etc.
So registration must be derived; the algorithm is DB-free for US aircraft.

## Backend `backend/adsb_enrich.py` (pure, unit-tested)

- `icao_to_registration(hex) -> str | None`: the deterministic FAA N-number
  algorithm for US ICAO addresses `0xA00001..0xADF7C7`. Verified anchors:
  `A00001 -> "N1"`, `ADF7C7 -> "N99999"`. Returns None outside the US block.
  Suffix uses the 24-letter set (no I/O); buckets 101711/10111/951/35 sum to the
  exact US block size (101711*9 = 915399).
- `icao_country(hex) -> str | None`: country from the ICAO 24-bit allocation
  blocks (a static subset table covering common North American + European
  traffic; None for unlisted blocks).

Wired in `_emit_adsb`: for each aircraft, attach `registration` (prefer readsb's
`r` if ever present, else the algorithm) and `country` (from the hex). Pass
through readsb's `t` (type) if present.

## Front end (ADS-B map)

- **Range rings**: Leaflet circles centered on the receiver (`rx_lat`/`rx_lon`,
  GPS-fillable) at 50 / 100 / 150 nm, with small distance labels. Only drawn
  when a receiver position is known.
- **Per-aircraft distance + bearing** from the receiver (haversine, client-side),
  shown with callsign, hex, registration, country, altitude, and speed in the
  aircraft tooltip. All text escaped.
- **Stats**: extend "X tracked / Y with position" with message rate (msgs/sec
  from the `meta.messages` delta over time) and max range (farthest aircraft).

## Out of scope (later / other sub-features)

Full aircraft-type database (heavy DB file; we surface readsb's `t` only).
Persistent aircraft history (belongs to the device-inventory sub-feature). Full
ICAO country table (we ship a common-traffic subset).

## Testing

- `tests/test_adsb_enrich.py` (hardware-free): N-number anchors (A00001->N1,
  ADF7C7->N99999) + a couple of mid-range values; non-US hex -> None; country
  lookup for US/Canada/UK/Germany blocks + unknown -> None.
- Live: run ADS-B, confirm overhead US aircraft show plausible N-numbers
  (cross-check one hex against a public lookup), range rings render around the
  GPS/located receiver, and the stats show a message rate and max range.
