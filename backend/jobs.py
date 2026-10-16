"""The one running HackRF job.

Modes live in this table. A kind that is not listed cannot be published,
and the status pill reads the label from the job the server sends.
Callers serialize access with the app state lock. This module does not
take that lock itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Mode:
    kind: str
    status_label: str
    uses_device: bool
    death_label: str


MODES: dict[str, Mode] = {
    "sweep": Mode("sweep", "Sweeping", True, "Sweep"),
    "decode": Mode("decode", "Decoding", True, "Decoder"),
    "capture": Mode("capture", "Recording", True, "Capture"),
    "adsb": Mode("adsb", "Tracking", True, "ADS-B"),
    "scan": Mode("scan", "Auto-Scanning", True, "Auto-Scan"),
    "radio": Mode("radio", "Listening", True, "Radio"),
    "scan_radio": Mode("scan_radio", "Scanning", True, "Scanner"),
    "replay": Mode("replay", "Replaying", False, "Replay"),
    "iq_play": Mode("iq_play", "Listening", False, "Playback"),
    "decode_file": Mode("decode_file", "Decoding", False, "Decode"),
}


def mode_spec(kind: str) -> Mode:
    try:
        return MODES[kind]
    except KeyError:
        raise ValueError(f"unknown job kind {kind!r}") from None


@dataclass
class Job:
    kind: str
    uses_device: bool
    status_label: str
    worker: object
    stop: Callable[[], None]
    identity: int


class JobBoard:
    def __init__(self) -> None:
        self.current: Job | None = None
        self._next_identity = 0

    def take_identity(self) -> int:
        self._next_identity += 1
        return self._next_identity

    def mode(self) -> str:
        return self.current.kind if self.current else "idle"

    def status_label(self) -> str:
        return self.current.status_label if self.current else "Idle"

    def uses_device(self) -> bool:
        return bool(self.current and self.current.uses_device)

    def worker(self, kind: str):
        if self.current and self.current.kind == kind:
            return self.current.worker
        return None

    def publish(self, kind: str, worker: object, stop: Callable[[], None], identity: int) -> Job:
        spec = mode_spec(kind)
        job = Job(
            kind=spec.kind,
            uses_device=spec.uses_device,
            status_label=spec.status_label,
            worker=worker,
            stop=stop,
            identity=identity,
        )
        self.current = job
        return job

    def clear(self) -> Job | None:
        job = self.current
        self.current = None
        return job

    def finish(self, identity: int) -> Job | None:
        if self.current is None or self.current.identity != identity:
            return None
        job = self.current
        self.current = None
        return job
