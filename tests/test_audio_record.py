"""Tests for WAV audio recording (backend.audio_record). Hardware-free."""

import os
import sys
import tempfile
import traceback
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import json  # noqa: E402

from backend.audio_record import (  # noqa: E402
    WavRecorder,
    finalize_recording,
    start_recording,
)


def _pcm(n):
    return np.arange(n, dtype=np.int16).tobytes()  # n mono 16-bit frames


def test_writes_valid_wav():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "a.wav")
    r = WavRecorder(p, 50000)
    r.write(_pcm(1000))
    r.close()
    with wave.open(p, "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 50000
        assert wf.getnframes() == 1000
    assert r.frames == 1000
    assert abs(r.duration_s - 1000 / 50000) < 1e-9
    assert r.file_size > 0


def test_two_chunks_accumulate():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "b.wav")
    r = WavRecorder(p, 8000)
    r.write(_pcm(100))
    r.write(_pcm(50))
    r.close()
    assert r.frames == 150
    with wave.open(p, "rb") as wf:
        assert wf.getnframes() == 150


def test_write_after_close_is_noop():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "c.wav")
    r = WavRecorder(p, 8000)
    r.write(_pcm(10))
    r.close()
    r.write(_pcm(10))   # must not crash or add frames
    assert r.frames == 10


def test_empty_write_is_safe():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "d.wav")
    r = WavRecorder(p, 8000)
    r.write(b"")
    r.close()
    assert r.frames == 0


def test_start_and_finalize_recording():
    d = tempfile.mkdtemp()
    now = 1_700_000_000
    geo = {"lat": 38.72, "lon": -77.81, "source": "gpsd"}
    rec, record = start_recording(d, now, 101_100_000, "fm", 50000, "DC101", geo)
    assert record.kind == "audio"
    assert record.name.endswith(".wav")
    assert record.geolocation == geo
    # Sidecar exists with the audio metadata before any audio is written.
    side = json.loads(open(record.sidecar).read())
    assert side["kind"] == "audio" and side["demod"] == "fm"
    assert side["finished_at"] is None

    rec.write(_pcm(25000))   # 0.5 s at 50 kHz
    finalize_recording(rec, record, now + 1)
    side2 = json.loads(open(record.sidecar).read())
    assert side2["finished_at"] == now + 1
    assert abs(side2["duration_s"] - 0.5) < 1e-6
    assert side2["file_size"] > 0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
