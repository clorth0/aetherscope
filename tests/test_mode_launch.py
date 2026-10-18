"""Mode-launch recipes: characterization of all ten _start_* in app.py.

Fake workers + a socket recorder pin, per mode: the JobBoard kind it
publishes, the started socket events (names + payloads byte-identical),
the finish derivation (status/toast/socket order), and the guard/error
toasts. No hardware, no real subprocesses, no store: _emit_captures_list
is replaced by a marker so its ordering against capture_done/status is
still pinned.

Inventory pinned here (backend/app.py on origin/main f8f80cd, lines
479-711): 5 _exit_handler modes (sweep, decode, adsb, radio,
scan_radio), 4 hand-built on_done modes (capture, replay, iq_play,
decode_file), 1 scan-event mode (scan). That is 10 _start_* / 10 finish
derivations; the review's "6 _exit_handler uses" is 5 on this tree.
"""

import os
import sys
import traceback
from contextlib import ExitStack
from dataclasses import dataclass
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import app  # noqa: E402
from backend.adsb import AdsbConfig  # noqa: E402
from backend.capture import CaptureConfig  # noqa: E402
from backend.decoders import DecodeConfig  # noqa: E402
from backend.radio import RadioConfig, ScanRadioConfig  # noqa: E402
from backend.scan import ScanConfig  # noqa: E402
from backend.sdr import SweepConfig  # noqa: E402

WORKER_NAMES = ["SweepStreamer", "Rtl433Decoder", "IqCapture", "AdsbReceiver",
                "RadioReceiver", "RadioScanner", "IqReplay", "IqAudioPlayer",
                "IqFileDecoder", "AutoScanner"]


@dataclass
class FakeRecord:
    name: str = "cap.iq"
    file_size: int = 1024 * 1024


class FakeWorker:
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.record = FakeRecord()
        self.stopped = False
        FakeWorker.instances.append(self)

    def start(self):
        return True

    def stop(self):
        self.stopped = True


class Harness:
    def __enter__(self):
        self.events = []
        self.stack = ExitStack()
        FakeWorker.instances = []
        emit = lambda name, payload=None: self.events.append((name, payload))  # noqa: E731
        self.stack.enter_context(mock.patch.object(app.socketio, "emit", emit))
        for name in WORKER_NAMES:
            self.stack.enter_context(mock.patch.object(app, name, FakeWorker))
        self.entry = SimpleNamespace(path="/tmp/cap.iq", freq_hz=100_000_000,
                                     sample_rate=2_000_000, exists=True)
        self.stack.enter_context(mock.patch.object(
            app, "find_capture", lambda name, captures_dir: self.entry))
        self.stack.enter_context(mock.patch.object(app, "_receiver_geotag", lambda: None))
        self.stack.enter_context(mock.patch.object(
            app, "_emit_captures_list", lambda: self.events.append(("captures_list", {}))))
        with app._state_lock:
            app._jobs.clear()
        app._device["info"] = {"serial": "test"}
        app._audio_rec["recorder"] = None
        app._audio_rec["record"] = None
        return self

    def __exit__(self, *exc):
        with app._state_lock:
            app._jobs.clear()
        self.stack.close()
        return False

    def names(self):
        return [n for n, _ in self.events]

    def clear(self):
        self.events.clear()


def _start_all(h):
    """(kind, start-callable, callback-kwarg) for every mode."""
    return [
        ("sweep", lambda: app._start_sweep(SweepConfig()), "on_exit"),
        ("decode", lambda: app._start_decode(DecodeConfig()), "on_exit"),
        ("capture", lambda: app._start_capture(CaptureConfig()), "on_done"),
        ("adsb", lambda: app._start_adsb(AdsbConfig()), "on_exit"),
        ("radio", lambda: app._start_radio(RadioConfig()), "on_exit"),
        ("scan_radio", lambda: app._start_scan_radio(ScanRadioConfig(freqs_mhz=[101.1])), "on_exit"),
        ("replay", lambda: app._start_replay("cap.iq"), "on_done"),
        ("iq_play", lambda: app._start_iq_play("cap.iq", "fm", 0.0), "on_done"),
        ("decode_file", lambda: app._start_decode_file("cap.iq"), "on_done"),
        ("scan", lambda: app._start_scan(ScanConfig()), "on_event"),
    ]


EXPECTED_STARTED = {
    "sweep": ["status", "status"],
    "decode": ["status", "status"],
    "capture": ["status", "status", "capture_started"],
    "adsb": ["status", "status"],
    "radio": ["status", "status", "radio_started"],
    "scan_radio": ["status", "status", "scan_radio_started"],
    "replay": ["status", "status", "replay_started"],
    "iq_play": ["status", "status", "radio_started", "iq_play_started"],
    "decode_file": ["status", "status", "decode_file_started", "toast"],
    "scan": ["status", "status"],
}


def test_all_ten_modes_publish_and_emit_started():
    with Harness() as h:
        cases = _start_all(h)
        assert len(cases) == 10, [c[0] for c in cases]
        for kind, start, cb in cases:
            h.clear(); FakeWorker.instances = []
            assert start() is True, kind
            assert app._jobs.mode() == kind, (kind, app._jobs.mode())
            assert h.names() == EXPECTED_STARTED[kind], (kind, h.events)
            assert len(FakeWorker.instances) == 1, kind
            worker = FakeWorker.instances[0]
            assert callable(worker.kwargs.get(cb)), (kind, cb, worker.kwargs)
            # status payload carries the board's mode, byte-identical labels
            statuses = [p for n, p in h.events if n == "status"]
            assert statuses[-1]["mode"] == kind, (kind, statuses[-1])


def test_started_payloads_are_byte_identical():
    with Harness() as h:
        app._start_capture(CaptureConfig())
        assert ("capture_started", {"record": {"name": "cap.iq", "file_size": 1048576}}) in h.events
        h.clear(); app._start_radio(RadioConfig(freq_mhz=101.1, demod="fm"))
        assert h.events[-1] == ("radio_started", {"sample_rate": app.AUDIO_RATE, "freq_mhz": 101.1, "demod": "fm"})
        h.clear(); app._start_scan_radio(ScanRadioConfig(freqs_mhz=[101.1, 102.2], demod="nfm"))
        assert h.events[-1] == ("scan_radio_started", {"count": 2, "sample_rate": app.AUDIO_RATE, "demod": "nfm"})
        h.clear(); app._start_replay("cap.iq")
        assert h.events[-1] == ("replay_started", {"name": "cap.iq", "freq_hz": 100_000_000, "sample_rate": 2_000_000})
        h.clear(); app._start_iq_play("cap.iq", "bogus", 500.0)
        # demod coercion + both started events, in order
        assert h.events[-2] == ("radio_started", {"sample_rate": app.AUDIO_RATE, "freq_mhz": 100.0, "demod": "fm"})
        assert h.events[-1] == ("iq_play_started", {"name": "cap.iq", "demod": "fm", "offset_hz": 500.0, "center_hz": 100_000_000, "sample_rate": 2_000_000})
        h.clear(); app._start_decode_file("cap.iq")
        assert h.events[-2] == ("decode_file_started", {"name": "cap.iq", "freq_hz": 100_000_000, "sample_rate": 2_000_000})
        assert h.events[-1] == ("toast", {"level": "info", "message": "Decoding cap.iq (this can take a moment)"})


def test_exit_modes_finish_derivation():
    death = {"sweep": "Sweep", "decode": "Decoder", "adsb": "ADS-B",
             "radio": "Radio", "scan_radio": "Scanner"}
    with Harness() as h:
        for kind, start, _cb in _start_all(h):
            if kind not in death:
                continue
            assert start() is True, kind
            on_exit = FakeWorker.instances[-1].kwargs["on_exit"]
            h.clear(); on_exit("stopped")
            assert h.events == [] and app._jobs.mode() == kind, (kind, h.events)
            on_exit("died")
            assert h.names() == ["toast", "status"], (kind, h.events)
            assert h.events[0] == ("toast", {"level": "error", "message": f"{death[kind]} stopped unexpectedly."})
            assert h.events[1][1]["mode"] == "idle"
            assert app._jobs.mode() == "idle"


def test_done_modes_finish_derivation():
    with Harness() as h:
        # capture: toast first, then capture_done, captures list, status
        app._start_capture(CaptureConfig()); rec = FakeRecord()
        on_done = FakeWorker.instances[-1].kwargs["on_done"]; h.clear()
        on_done(rec, "completed")
        assert h.events == [
            ("toast", {"level": "info", "message": "Capture done: cap.iq (1.0 MB)"}),
            ("capture_done", {"record": {"name": "cap.iq", "file_size": 1048576}, "reason": "completed"}),
            ("captures_list", {}), ("status", h.events[-1][1])], h.events
        assert h.events[-1][1]["mode"] == "idle"
        app._start_capture(CaptureConfig()); on_done = FakeWorker.instances[-1].kwargs["on_done"]; h.clear()
        on_done(rec, "cancelled")
        assert h.events[0] == ("toast", {"level": "warn", "message": "Capture cancelled: cap.iq"})
        app._start_capture(CaptureConfig()); on_done = FakeWorker.instances[-1].kwargs["on_done"]; h.clear()
        on_done(rec, "died")
        assert h.events[0] == ("toast", {"level": "error", "message": "Capture failed: cap.iq"})
        # replay / iq_play: done event then status, no toast
        for kind, start, event in [("replay", lambda: app._start_replay("cap.iq"), "replay_done"),
                                   ("iq_play", lambda: app._start_iq_play("cap.iq", "fm", 0.0), "iq_play_done")]:
            assert start() is True
            cb = FakeWorker.instances[-1].kwargs["on_done"]; h.clear(); cb("completed")
            assert h.names() == [event, "status"], (kind, h.events)
            assert h.events[0] == (event, {"reason": "completed", "name": "cap.iq"})
        # decode_file: done, completed-only toast, status
        app._start_decode_file("cap.iq"); cb = FakeWorker.instances[-1].kwargs["on_done"]; h.clear()
        cb("completed")
        assert h.names() == ["decode_file_done", "toast", "status"], h.events
        assert h.events[1] == ("toast", {"level": "info", "message": "Decode finished: cap.iq"})
        app._start_decode_file("cap.iq"); cb = FakeWorker.instances[-1].kwargs["on_done"]; h.clear()
        cb("died")
        assert h.names() == ["decode_file_done", "status"], h.events


def test_scan_finish_derivation():
    with Harness() as h:
        app._start_scan(ScanConfig())
        on_event = FakeWorker.instances[-1].kwargs["on_event"]; h.clear()
        on_event("scan_progress", {"pct": 1})
        assert h.events == [("scan_progress", {"pct": 1})] and app._jobs.mode() == "scan"
        on_event("scan_completed", {"summary": {}})
        assert h.names() == ["scan_progress", "scan_completed", "status"], h.events
        assert app._jobs.mode() == "idle"
        app._start_scan(ScanConfig()); on_event = FakeWorker.instances[-1].kwargs["on_event"]; h.clear()
        on_event("scan_failed", {"reason": "exception"})
        assert h.names() == ["scan_failed", "status"] and app._jobs.mode() == "idle"


def test_guards_and_error_paths():
    with Harness() as h:
        # device missing: device modes refused, file modes allowed
        app._device["info"] = None; h.clear()
        assert app._start_sweep(SweepConfig()) is False
        assert h.events == [("toast", {"level": "error", "message": "Cannot start: HackRF not detected."})]
        assert app._start_replay("cap.iq") is True
        app._device["info"] = {"serial": "test"}
        # scan_radio pre-guard runs before the board is touched
        h.clear(); assert app._start_scan_radio(ScanRadioConfig(freqs_mhz=[])) is False
        assert h.events == [("toast", {"level": "error", "message": "No marked frequencies to scan."})]
        # invalid / missing captures
        for call, msg in [(lambda: app._start_replay("../cap.iq"), "Invalid capture name"),
                          (lambda: app._start_iq_play("cap.wav", "fm", 0.0), "Invalid capture"),
                          (lambda: app._start_decode_file("cap.wav"), "Invalid capture")]:
            h.clear(); assert call() is False
            assert h.events == [("toast", {"level": "error", "message": msg})], h.events
        h.entry = None
        for call in [lambda: app._start_replay("cap.iq"), lambda: app._start_iq_play("cap.iq", "fm", 0.0),
                     lambda: app._start_decode_file("cap.iq")]:
            h.clear(); assert call() is False
            assert h.events == [("toast", {"level": "error", "message": "Capture not found"})], h.events




# --- the single-recipe property (backend/launch.py) ---------------------

class _SpecWorker:
    def __init__(self, ctx):
        self.ctx = ctx

    def start(self):
        return True

    def stop(self):
        pass


class _FakeHost:
    def __init__(self):
        self.events = []
        self.deaths = 0
        self.current = None
        self._next = 0

    def begin_job(self, kind, make):
        self._next += 1
        worker = make(self._next)
        if worker.start() is not True:
            return False, None
        self.current = (kind, self._next, worker)
        self.status()
        return True, worker

    def finish_job(self, identity):
        if self.current is None or self.current[1] != identity:
            return False
        self.current = None
        return True

    def emit(self, name, payload):
        self.events.append((name, payload))

    def toast(self, level, message):
        self.events.append(("toast", {"level": level, "message": message}))

    def status(self):
        mode = self.current[0] if self.current else "idle"
        self.events.append(("status", {"mode": mode}))

    def captures_list(self):
        self.events.append(("captures_list", {}))

    def device_present(self):
        return True

    def bump_deaths(self):
        self.deaths += 1

    def death_label(self, kind):
        return {"test_mode": "Test", "a": "A", "b": "B"}.get(kind, kind)


def test_test_only_mode_spec_launches_through_same_path():
    from backend.launch import LaunchSpec, Launcher, exit_finish
    host = _FakeHost()
    launcher = Launcher(host)
    spec = LaunchSpec(
        kind="test_mode",
        build=lambda ctx: _SpecWorker(ctx),
        finish=exit_finish(),
        started=lambda worker: [("emit", "test_started", {"ok": True})],
    )
    assert launcher.launch(spec) is True
    assert host.current[0] == "test_mode"
    assert ("test_started", {"ok": True}) in host.events
    worker = host.current[2]
    host.events.clear()
    worker.ctx.on_exit("stopped")
    assert host.events == [] and host.current is not None
    worker.ctx.on_exit("died")
    assert host.events == [("toast", {"level": "error", "message": "Test stopped unexpectedly."}),
                           ("status", {"mode": "idle"})], host.events
    assert host.deaths == 1 and host.current is None


def test_shared_done_finish_derivation_across_modes():
    from backend.launch import LaunchSpec, Launcher, done_finish
    for event in ("a_done", "b_done"):
        host = _FakeHost(); launcher = Launcher(host)
        spec = LaunchSpec(
            kind="a" if event == "a_done" else "b",
            build=lambda ctx: _SpecWorker(ctx),
            finish=done_finish(event, lambda extra, reason: {"reason": reason, "name": "x"},
                               toast_for=lambda reason, extra, device: ("info", "fin") if reason == "completed" else None,
                               toast_before_event=False),
        )
        assert launcher.launch(spec) is True
        worker = host.current[2]; host.events.clear()
        worker.ctx.on_done("completed")
        assert [n for n, _ in host.events] == [event, "toast", "status"], host.events


def test_app_has_one_launch_path_and_no_handbuilt_finish_closures():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "backend" / "app.py").read_text()
    assert source.count("_launcher.launch(") == 10, source.count("_launcher.launch(")
    assert "_exit_handler" not in source
    assert "def on_done" not in source
    assert "def on_scan_event" not in source


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"PASS {t.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {t.__name__}"); traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
