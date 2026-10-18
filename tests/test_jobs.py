"""One current job, server-sent status labels, and a consistent audio snapshot."""

import os
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import app  # noqa: E402
from backend.jobs import JobBoard, MODES, mode_spec  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class _Worker:
    def __init__(self, *, fail=False, value="ok"):
        self.fail = fail
        self.value = value
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        if self.fail:
            raise RuntimeError("start failed")
        return self.value

    def stop(self):
        self.stopped = True


def _reset():
    with app._state_lock:
        app._jobs.clear()
    app._device["info"] = {"serial": "test"}
    app._audio_rec["recorder"] = None
    app._audio_rec["record"] = None


def test_modes_cover_listening_and_file_jobs():
    assert MODES["radio"].status_label == "Listening"
    assert MODES["radio"].uses_device is True
    assert MODES["iq_play"].status_label == "Listening"
    assert MODES["iq_play"].uses_device is False
    assert MODES["decode_file"].status_label == "Decoding"
    assert MODES["decode_file"].uses_device is False
    assert MODES["replay"].uses_device is False
    try:
        mode_spec("not-a-mode")
    except ValueError:
        return
    raise AssertionError("unknown kind was accepted")


def test_stale_finish_does_not_clear_the_new_job():
    board = JobBoard()
    first = board.take_identity()
    board.publish("replay", object(), lambda: None, first)
    second = board.take_identity()
    board.publish("radio", object(), lambda: None, second)
    assert board.finish(first) is None
    assert board.mode() == "radio"
    assert board.status_label() == "Listening"
    assert board.uses_device() is True
    assert board.finish(second).kind == "radio"
    assert board.mode() == "idle"
    assert board.status_label() == "Idle"


def test_failed_start_leaves_the_board_idle_and_stops_the_worker():
    _reset()
    worker = _Worker(fail=True)

    def make(_identity):
        return worker

    ok, value = app._begin_job("radio", make)
    assert ok is False
    assert value is None
    assert worker.started is True
    assert worker.stopped is True
    assert app._jobs.mode() == "idle"
    assert app._jobs.status_label() == "Idle"


def test_file_job_does_not_own_the_device():
    _reset()
    app._device["info"] = None
    worker = _Worker(value=True)

    def make(_identity):
        return worker

    ok, value = app._begin_job("replay", make)
    assert ok is True
    # Uniform contract: _begin_job hands back the worker, not a
    # mode-specific start() return value (capture's record used to leak here).
    assert value is worker
    assert app._jobs.mode() == "replay"
    assert app._jobs.status_label() == "Replaying"
    assert app._jobs.uses_device() is False
    fields = app._status_fields_locked()
    assert fields["mode"] == "replay"
    assert fields["status_label"] == "Replaying"


def test_device_job_requires_a_device_and_a_later_finish_is_ignored():
    _reset()
    app._device["info"] = None
    ok, _value = app._begin_job("sweep", lambda _identity: _Worker())
    assert ok is False
    assert app._jobs.mode() == "idle"

    app._device["info"] = {"serial": "test"}
    first = _Worker()
    ok, _value = app._begin_job("radio", lambda _identity: first)
    assert ok is True
    assert app._jobs.status_label() == "Listening"
    assert app._jobs.uses_device() is True
    stale = app._jobs.current.identity
    second = _Worker()
    ok, _value = app._begin_job("sweep", lambda _identity: second)
    assert ok is True
    assert first.stopped is True
    assert app._jobs.mode() == "sweep"
    assert app._jobs.status_label() == "Sweeping"
    assert app._finish_job(stale) is False
    assert app._jobs.mode() == "sweep"


def test_audio_snapshot_reads_both_fields_together():
    _reset()

    class _Record:
        name = "clip.wav"

    app._audio_rec["recorder"] = object()
    app._audio_rec["record"] = _Record()
    assert app._audio_status_snapshot() == {"recording": True, "name": "clip.wav"}
    source = (ROOT / "backend" / "app.py").read_text()
    assert '_audio_rec["record"]' not in source.split("def on_connect")[1].split("def _filter_payload")[0]


def test_status_pill_prints_the_server_label():
    js = (ROOT / "frontend" / "static" / "waterfall.js").read_text()
    body = re.search(r"function refreshStatusUI\(\) \{([\s\S]*?)\n\}", js).group(1)
    assert "statusText.textContent = serverStatusLabel" in body
    assert 'serverMode === "sweep"' not in body
    assert "s.status_label" in js
    app_source = (ROOT / "backend" / "app.py").read_text()
    assert '_state["mode"]' not in app_source
    assert "for slot in" not in app_source


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
