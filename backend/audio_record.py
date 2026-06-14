"""WAV recording of demodulated radio audio.

Tees the int16 PCM that `RadioReceiver` already produces into a WAV file (stdlib
`wave`, no dependency), with a JSON sidecar that mirrors `CaptureRecord` closely
enough for the capture list to render it. Audio recordings can be geotagged like
IQ captures; SigMF does not apply (it describes complex IQ, not audio).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class AudioRecord:
    """Sidecar metadata for one audio recording (kind == 'audio')."""
    kind: str
    name: str
    path: str
    sidecar: str
    freq_hz: int
    demod: str
    audio_rate: int
    started_at: float
    finished_at: float | None
    file_size: int
    duration_s: float
    label: str
    geolocation: dict | None = None


class WavRecorder:
    """Thread-safe mono 16-bit WAV writer fed PCM from the radio audio callback."""

    def __init__(self, path, rate, channels: int = 1, sampwidth: int = 2):
        self.path = str(path)
        self.rate = int(rate)
        self.channels = int(channels)
        self.sampwidth = int(sampwidth)
        self._lock = threading.Lock()
        self._frames = 0
        self._closed = False
        self._wf = wave.open(self.path, "wb")
        self._wf.setnchannels(self.channels)
        self._wf.setsampwidth(self.sampwidth)
        self._wf.setframerate(self.rate)

    def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            if self._closed:
                return
            self._wf.writeframes(pcm)
            self._frames += len(pcm) // (self.sampwidth * self.channels)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._wf.close()
            except Exception:
                pass

    @property
    def frames(self) -> int:
        return self._frames

    @property
    def duration_s(self) -> float:
        return self._frames / self.rate if self.rate else 0.0

    @property
    def file_size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0


def _slug(label: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", (label or "").strip())
    return s.strip("_") or "audio"


def start_recording(captures_dir, now, freq_hz, demod, audio_rate, label, geolocation):
    """Open a WAV recorder + write the initial sidecar. Returns (recorder, record)."""
    captures_dir = Path(captures_dir)
    captures_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(now))
    tag = _slug(label) if label else f"{int(freq_hz) // 1_000_000}MHz"
    base = f"{ts}_{tag}"
    path = captures_dir / f"{base}.wav"
    sidecar = captures_dir / f"{base}.json"
    rec = WavRecorder(path, audio_rate)
    record = AudioRecord(
        kind="audio", name=path.name, path=str(path), sidecar=str(sidecar),
        freq_hz=int(freq_hz), demod=demod, audio_rate=int(audio_rate),
        started_at=now, finished_at=None, file_size=0, duration_s=0.0,
        label=label or "", geolocation=geolocation,
    )
    sidecar.write_text(json.dumps(asdict(record), indent=2))
    return rec, record


def finalize_recording(rec: WavRecorder, record: AudioRecord, now) -> AudioRecord:
    """Close the recorder and rewrite the sidecar with final duration/size."""
    rec.close()
    record.finished_at = now
    record.duration_s = rec.duration_s
    record.file_size = rec.file_size
    try:
        Path(record.sidecar).write_text(json.dumps(asdict(record), indent=2))
    except OSError:
        pass
    return record
