"""Lightweight runtime telemetry: subprocess warnings + counters.

Surfaces the diagnostics the SDR subprocesses print to stderr (which were
previously discarded), plus a few server counters, so the UI can show whether
the device/USB/CPU is keeping up. Thread-safe; cheap.
"""

from __future__ import annotations

import re
import threading
from typing import IO

_lock = threading.Lock()
_counters: dict[str, int] = {}
_recent: list[str] = []
_MAX_RECENT = 50

# Lines worth surfacing from hackrf/rtl/readsb stderr (drops, overruns, errors).
_WARN_RE = re.compile(r"overrun|under-?run|drop|fail|error|couldn't|warning", re.I)


def is_warning(line: str) -> bool:
    return bool(_WARN_RE.search(line))


def bump(key: str, n: int = 1) -> None:
    with _lock:
        _counters[key] = _counters.get(key, 0) + n


def note_warning(source: str, line: str) -> None:
    line = line.strip()
    if not line:
        return
    with _lock:
        _counters["usb_warnings"] = _counters.get("usb_warnings", 0) + 1
        _recent.append(f"{source}: {line[:140]}")
        if len(_recent) > _MAX_RECENT:
            _recent.pop(0)


def snapshot() -> dict:
    with _lock:
        return {"counters": dict(_counters), "recent": list(_recent)}


def reset() -> None:
    with _lock:
        _counters.clear()
        _recent.clear()


def watch_stderr(source: str, stream: "IO") -> threading.Thread:
    """Read a subprocess's stderr in a daemon thread, recording only the lines
    that look like warnings/errors. Accepts text or byte streams.
    """
    def run() -> None:
        try:
            for raw in stream:
                line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
                if is_warning(line):
                    note_warning(source, line)
        except Exception:
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
