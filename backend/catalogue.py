"""Capture catalogue: the one home for the capture sidecar shape.

Deep module (see docs/architecture.md). Behind this small interface —
``list_catalogue``, ``find_capture``, ``parse_capture``, plus the name
helpers — callers stop knowing that a capture is a quartet of files
(.iq/.wav + .json sidecar + .sigmf-meta) or which keys its sidecar has.

Two named shapes, deliberately not one: an IQ capture and an audio
recording share a list but not a shape. IQ carries ``sample_rate`` /
``sample_format`` and may have a SigMF companion; audio carries
``demod`` / ``audio_rate``, sets ``kind="audio"``, and never has SigMF.
``IqCaptureEntry`` and ``AudioCaptureEntry`` share only the fields both
really have (name/path/sidecar/freq/times/size/duration/label/geotag).

Invariants callers may rely on:
- ``freq_hz`` is an int (0 when the sidecar lacks it) and an IQ entry's
  ``sample_rate`` is an int (2_000_000 when absent) — the defaults the
  replay/play/decode start functions used to re-derive per call site.
- ``to_dict()`` reproduces the sidecar dict exactly (same keys, same
  order), so the on-disk shape is preserved. ``geolocation_redacted``
  round-trips only when the sidecar actually carries it.
- ``to_wire_dict()`` / ``wire_capture_dict()`` are the only shapes that
  cross to a client: they drop the absolute ``path``/``sidecar``
  filesystem locations (the frontend builds download/play URLs from
  ``name``/``sigmf_name``, never those fields). Server code keeps using
  the typed entry's ``path`` internally.
- ``resolve_capture_file()`` is the one name->path resolution for
  serving a capture file: flat capture names only, and the resolved
  file must stay directly inside the captures directory (symlink
  escapes included), or there is no file to serve.
- The SigMF companion name is derived here and only here
  (``sigmf_meta_name`` / ``entry.sigmf_name``); the frontend consumes the
  server-provided ``sigmf_name`` instead of deriving it again.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

log = logging.getLogger(__name__)

# Captures land next to the repo by default; override with AETHERSCOPE_CAPTURES_DIR
_REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURES_DIR = Path(os.environ.get("AETHERSCOPE_CAPTURES_DIR") or (_REPO_ROOT / "captures"))

_CAPTURE_SUFFIXES = (".iq", ".wav", ".json", ".sigmf-meta")
_SERVED_SUFFIXES = (".iq", ".wav", ".sigmf-meta")
_DEFAULT_SAMPLE_RATE = 2_000_000


def slugify(label: str | None, fallback: str = "capture") -> str:
    """Filename slug for a capture label; one home for the old _slug copies.

    The capture copy fell back to "capture", the audio copy to "audio"
    (and tolerated None); the fallback is the only real difference.
    """
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", (label or "").strip())
    return s.strip("_") or fallback


def capture_basename(name: str) -> str | None:
    """Return the capture stem, or None when the name has a separator or '..'.

    One suffix is stripped, in the order .iq, .wav, .json, .sigmf-meta.
    Callers still decide which suffix they accept.
    """
    if not isinstance(name, str) or "/" in name or ".." in name:
        return None
    base = name
    for suffix in _CAPTURE_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if not base or base in (".", ".."):
        return None
    return base


def sigmf_meta_name(name: str) -> str | None:
    """The SigMF companion filename for an .iq capture, else None.

    The single derivation: replaces name[:-3] in app.py, removesuffix
    in capture.py, and slice(0, -3) in waterfall.js.
    """
    if not isinstance(name, str) or not name.endswith(".iq"):
        return None
    base = capture_basename(name)
    return f"{base}.sigmf-meta" if base else None


def _as_int(value, default: int) -> int:
    try:
        return int(value) if value else default
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


_WIRE_PATH_FIELDS = ("path", "sidecar")


def wire_capture_dict(data: dict) -> dict:
    """A capture dict safe to send to a client: no absolute fs paths."""
    return {k: v for k, v in data.items() if k not in _WIRE_PATH_FIELDS}


class _Entry:
    """Behaviour both capture shapes share; not itself a capture shape."""

    kind: ClassVar[str] = "iq"
    name: str
    path: str

    @property
    def sigmf_name(self) -> str | None:
        return sigmf_meta_name(self.name) if self.kind == "iq" else None

    @property
    def exists(self) -> bool:
        return bool(self.path) and Path(self.path).exists()

    def to_wire_dict(self) -> dict:
        return wire_capture_dict(self.to_dict())

    def missing_in(self, captures_dir) -> bool:
        return not (Path(captures_dir) / self.name).exists() if self.name else True

    def sigmf_exists_in(self, captures_dir) -> bool:
        meta = self.sigmf_name
        return bool(meta and (Path(captures_dir) / meta).exists())


@dataclass
class IqCaptureEntry(_Entry):
    """One IQ capture, as written by capture.CaptureRecord's sidecar."""

    kind: ClassVar[str] = "iq"
    name: str = ""
    path: str = ""
    sidecar: str = ""
    freq_hz: int = 0
    sample_rate: int = _DEFAULT_SAMPLE_RATE
    duration_s: float = 0.0
    started_at: float = 0.0
    finished_at: float | None = None
    file_size: int = 0
    sample_format: str = "cs8"
    label: str = ""
    geolocation: dict | None = None

    def to_dict(self) -> dict:
        d = {
            "name": self.name, "path": self.path, "sidecar": self.sidecar,
            "freq_hz": self.freq_hz, "sample_rate": self.sample_rate,
            "duration_s": self.duration_s, "started_at": self.started_at,
            "finished_at": self.finished_at, "file_size": self.file_size,
            "sample_format": self.sample_format, "label": self.label,
            "geolocation": self.geolocation,
        }
        redacted = getattr(self, "_geolocation_redacted", None)
        if redacted is not None:
            d["geolocation_redacted"] = redacted
        return d


@dataclass
class AudioCaptureEntry(_Entry):
    """One WAV audio recording, as written by audio_record's sidecar."""

    kind: str = "audio"  # a real sidecar field here, unlike the IQ shape
    name: str = ""
    path: str = ""
    sidecar: str = ""
    freq_hz: int = 0
    demod: str = ""
    audio_rate: int = 0
    started_at: float = 0.0
    finished_at: float | None = None
    file_size: int = 0
    duration_s: float = 0.0
    label: str = ""
    geolocation: dict | None = None

    def to_dict(self) -> dict:
        d = {
            "kind": "audio", "name": self.name, "path": self.path,
            "sidecar": self.sidecar, "freq_hz": self.freq_hz,
            "demod": self.demod, "audio_rate": self.audio_rate,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "file_size": self.file_size, "duration_s": self.duration_s,
            "label": self.label, "geolocation": self.geolocation,
        }
        redacted = getattr(self, "_geolocation_redacted", None)
        if redacted is not None:
            d["geolocation_redacted"] = redacted
        return d


CaptureEntry = IqCaptureEntry | AudioCaptureEntry


def parse_capture(data: dict) -> CaptureEntry:
    """Parse one sidecar dict into its typed shape (audio iff kind=audio)."""
    common = {
        "name": str(data.get("name") or ""),
        "path": str(data.get("path") or ""),
        "sidecar": str(data.get("sidecar") or ""),
        "freq_hz": _as_int(data.get("freq_hz"), 0),
        "started_at": _as_float(data.get("started_at"), 0.0),
        "finished_at": (None if data.get("finished_at") is None
                        else _as_float(data.get("finished_at"), 0.0)),
        "file_size": _as_int(data.get("file_size"), 0),
        "duration_s": _as_float(data.get("duration_s"), 0.0),
        "label": str(data.get("label") or ""),
        "geolocation": data.get("geolocation"),
    }
    if data.get("kind") == "audio":
        entry: CaptureEntry = AudioCaptureEntry(
            **common, demod=str(data.get("demod") or ""),
            audio_rate=_as_int(data.get("audio_rate"), 0),
        )
    else:
        entry = IqCaptureEntry(
            **common,
            sample_rate=_as_int(data.get("sample_rate"), _DEFAULT_SAMPLE_RATE),
            sample_format=str(data.get("sample_format") or "cs8"),
        )
    if "geolocation_redacted" in data:
        entry._geolocation_redacted = data.get("geolocation_redacted")  # type: ignore[attr-defined]
    return entry


def _dir(captures_dir) -> Path:
    return Path(captures_dir) if captures_dir is not None else CAPTURES_DIR


def list_catalogue(captures_dir=None) -> list[CaptureEntry]:
    """All captures in the directory, newest first; bad sidecars skipped."""
    root = _dir(captures_dir)
    if not root.exists():
        return []
    items: list[CaptureEntry] = []
    for sidecar in root.glob("*.json"):
        try:
            data = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        entry = parse_capture(data)
        # refresh size from disk in case the run is still in flight
        try:
            entry.file_size = os.path.getsize(entry.path)
        except OSError:
            pass
        items.append(entry)
    items.sort(key=lambda e: e.started_at, reverse=True)
    return items


def find_capture(name: str, captures_dir=None) -> CaptureEntry | None:
    """One typed capture by exact filename, or None."""
    return next((e for e in list_catalogue(captures_dir) if e.name == name), None)


def resolve_capture_file(name: str, captures_dir=None) -> Path | None:
    """Resolve a client-supplied capture name to a file to serve, or None.

    Only flat names in a served capture form (``<base>.iq/.wav/
    .sigmf-meta``; the ``.json`` sidecar is internal and itself embeds
    absolute paths, so it is never served) are accepted, and the fully resolved file must sit
    directly inside the captures directory -- so ``../``, absolute,
    encoded-separator (already decoded by Flask routing), nested, and
    symlink escapes all resolve to None (the route answers 404).
    """
    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        return None
    if capture_basename(name) is None or not name.endswith(_SERVED_SUFFIXES):
        return None
    root = _dir(captures_dir).resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root or not candidate.is_file():
        return None
    return candidate


def redact_location(name: str, captures_dir=None) -> bool:
    """Strip the geotag from a capture's sidecar (sets geolocation to null and
    marks it redacted). Returns True if the sidecar was updated or already
    redacted. The samples never contain location, so this fully removes it.
    """
    root = _dir(captures_dir)
    base = capture_basename(name)
    if base is None:
        return False
    sc = root / f"{base}.json"
    try:
        data = json.loads(sc.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not (data.get("geolocation") is None and data.get("geolocation_redacted")):
        data["geolocation"] = None
        data["geolocation_redacted"] = True
        try:
            sc.write_text(json.dumps(data, indent=2))
        except OSError:
            log.exception("failed to redact %s", sc)
            return False
    # Scrub the companion SigMF meta too, so the standardized export is clean.
    meta = root / f"{base}.sigmf-meta"
    try:
        m = json.loads(meta.read_text())
        if m.get("global", {}).pop("core:geolocation", None) is not None:
            meta.write_text(json.dumps(m, indent=2))
    except (OSError, json.JSONDecodeError):
        pass
    return True


def delete_capture(name: str, captures_dir=None) -> bool:
    """Delete a capture quartet (.iq/.wav + sidecar + SigMF meta) by name."""
    root = _dir(captures_dir)
    base = capture_basename(name)
    if base is None:
        return False
    removed = False
    for suffix in _CAPTURE_SUFFIXES:
        p = root / f"{base}{suffix}"
        try:
            if p.exists():
                p.unlink()
                removed = True
        except OSError:
            log.exception("failed to delete %s", p)
    return removed
