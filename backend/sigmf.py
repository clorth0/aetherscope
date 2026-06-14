"""SigMF metadata for captures.

Builds a SigMF (https://sigmf.org) metadata object describing an Aetherscope
capture so the recording is portable to other SDR tools (GNU Radio, inspectrum,
IQEngine, the `sigmf` Python library, ...). No new dependency: the metadata is
plain JSON we emit ourselves.

Aetherscope keeps its `.iq` sample file and its UI sidecar; the companion
`.sigmf-meta` references the `.iq` via `core:dataset` (a SigMF non-conformant
dataset), so nothing about the existing capture flow changes. GPS geolocation,
when present, becomes a GeoJSON Point in `core:geolocation`.
"""

from __future__ import annotations

import datetime

SIGMF_VERSION = "1.0.0"
# cs8 interleaved I/Q (signed 8-bit) == SigMF complex int8.
SIGMF_DATATYPE = "ci8"


def _iso8601_utc(epoch) -> str | None:
    """System-clock epoch seconds -> ISO8601 UTC (with trailing Z), or None.

    Capture time comes from the system clock by design (the GPS puck has no PPS),
    so this is the authoritative capture datetime.
    """
    if epoch is None:
        return None
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def build_sigmf_meta(record: dict) -> dict:
    """Build a SigMF metadata dict from a capture record (CaptureRecord asdict)."""
    g = {
        "core:datatype": SIGMF_DATATYPE,
        "core:version": SIGMF_VERSION,
        "core:hw": "HackRF One",
        "core:recorder": "Aetherscope",
        "core:dataset": record.get("name", ""),   # NCD: the .iq sample file
    }
    sr = record.get("sample_rate")
    if sr:   # required + positive in SigMF; omit rather than emit 0
        g["core:sample_rate"] = float(sr)
    label = record.get("label")
    if label:
        g["core:description"] = label

    geo = record.get("geolocation")
    if geo and geo.get("lat") is not None and geo.get("lon") is not None:
        coords = [geo["lon"], geo["lat"]]              # GeoJSON is [lon, lat, alt]
        if geo.get("alt_m") is not None:
            coords.append(geo["alt_m"])
        g["core:geolocation"] = {"type": "Point", "coordinates": coords}

    cap = {"core:sample_start": 0}
    if record.get("freq_hz") is not None:
        cap["core:frequency"] = float(record["freq_hz"])
    dt = _iso8601_utc(record.get("started_at"))
    if dt:
        cap["core:datetime"] = dt

    return {"global": g, "captures": [cap], "annotations": []}
