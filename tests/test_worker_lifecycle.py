"""Worker lifecycle: one interface (start/stop/is_running) across all workers.

Characterization tests pin prior observable behaviour using fake
subprocesses (no hardware): start-guard refusal, terminate->wait->kill
escalation, died-vs-completed classification, ADS-B tempdir cleanup, and
the replay/playback event-only stop. The uniform return contract is
start() -> bool (True = started, False = refused because already
running); IqCapture's record is read from ``worker.record``.
"""

import os
import subprocess
import sys
import io
import tempfile
import threading
import traceback
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import adsb as adsb_mod  # noqa: E402
from backend import capture as capture_mod  # noqa: E402
from backend import decode_file as decode_mod  # noqa: E402
from backend import decoders as decoders_mod  # noqa: E402
from backend import iq_playback as iq_playback_mod  # noqa: E402
from backend import radio as radio_mod  # noqa: E402
from backend import replay as replay_mod  # noqa: E402
from backend import scan as scan_mod  # noqa: E402
from backend import sdr as sdr_mod  # noqa: E402
from backend.adsb import AdsbConfig, AdsbReceiver  # noqa: E402
from backend.capture import CaptureConfig, IqCapture  # noqa: E402
from backend.decode_file import IqFileDecoder  # noqa: E402
from backend.decoders import DecodeConfig, Rtl433Decoder  # noqa: E402
from backend.iq_playback import IqAudioPlayer  # noqa: E402
from backend.radio import RadioConfig, RadioReceiver, RadioScanner, ScanRadioConfig  # noqa: E402
from backend.replay import IqReplay  # noqa: E402
from backend.scan import AutoScanner, ScanConfig  # noqa: E402
from backend.sdr import SweepConfig, SweepStreamer  # noqa: E402


class FakeProc:
    """Records the teardown sequence; wait() optionally times out once."""

    def __init__(self, stdout_lines=(), timeout_on_wait=False, exited=False):
        self.calls = []
        # Radio/sweep read a byte stream; line decoders iterate. BytesIO
        # satisfies both (iterating yields byte lines, empty here).
        self.stdout = io.BytesIO(b"".join(
            (l if isinstance(l, bytes) else l.encode()) for l in stdout_lines))
        self.stderr = None
        self._timeout = timeout_on_wait
        self._exited = exited

    def poll(self):
        return 0 if self._exited else None

    def terminate(self):
        self.calls.append("terminate")

    def wait(self, timeout=None):
        self.calls.append(f"wait({timeout})")
        if self._timeout:
            self._timeout = False
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        self._exited = True
        return 0

    def kill(self):
        self.calls.append("kill")
        self._exited = True


class _Alive:
    def is_alive(self):
        return True


def _all_workers():
    workers = [
        SweepStreamer(SweepConfig(), on_sweep=lambda f, p: None),
        Rtl433Decoder(DecodeConfig(), on_event=lambda e: None),
        AdsbReceiver(AdsbConfig(), on_update=lambda a, m: None),
        RadioReceiver(RadioConfig(), on_audio=lambda b: None),
        RadioScanner(ScanRadioConfig(freqs_mhz=[101.1]), on_audio=lambda b: None,
                     on_event=lambda n, p: None),
        IqReplay("/nonexistent.iq", 100_000_000, 2_000_000, on_frame=lambda *a: None),
        IqAudioPlayer("/nonexistent.iq", 2_000_000, "fm", 0, on_audio=lambda b: None),
        IqFileDecoder("/nonexistent.iq", 2_000_000, 433_920_000, on_event=lambda e: None),
        AutoScanner(ScanConfig(), on_event=lambda n, p: None),
    ]
    return workers


def test_uniform_interface_and_start_guard_refusal():
    workers = _all_workers()
    # IqCapture is created separately (start does filesystem setup).
    workers.append(IqCapture(CaptureConfig(duration_s=0.1)))
    for w in workers:
        assert hasattr(w, "start") and hasattr(w, "stop") and hasattr(w, "is_running"), type(w)
        assert not hasattr(w, "cancel"), f"{type(w).__name__} still exposes cancel()"
        w._thread = _Alive()
        assert w.is_running() is True
        assert w.start() is False, f"{type(w).__name__} re-started while running"


def test_capture_start_returns_bool_and_record_is_exposed():
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(capture_mod, "CAPTURES_DIR", Path(td)):
            cap = IqCapture(CaptureConfig(duration_s=0.1, sample_rate=2_000_000))
            with mock.patch.object(capture_mod.subprocess, "Popen",
                                   return_value=FakeProc(exited=True)):
                assert cap.start() is True
                record = cap.record
                assert record is not None and record.name.endswith(".iq")
                assert cap.start() is False  # guard: no second thread/sidecar
                cap.stop()
                cap._thread.join(timeout=5)


def _check_escalation(worker, wait_timeout):
    fake = FakeProc(timeout_on_wait=True)
    worker._proc = fake
    worker.stop()
    assert fake.calls == ["terminate", f"wait({wait_timeout})", "kill"], (
        f"{type(worker).__name__}: {fake.calls}")
    assert worker._proc is None


def test_stop_escalation_order_across_subprocess_workers():
    sweep, decoder, adsb_w, radio_w, scanner = _all_workers()[:5]
    _check_escalation(sweep, 2)
    _check_escalation(decoder, 2)
    _check_escalation(adsb_w, 3)  # ADS-B waited 3 s before this collapse
    _check_escalation(radio_w, 2)
    _check_escalation(scanner, 2)
    cap = IqCapture(CaptureConfig(duration_s=0.1))
    _check_escalation(cap, 2)
    dec_file = IqFileDecoder("/x.iq", 2_000_000, 433_920_000, on_event=lambda e: None)
    _check_escalation(dec_file, 2)


def test_stop_without_timeout_does_not_kill():
    w = SweepStreamer(SweepConfig(), on_sweep=lambda f, p: None)
    fake = FakeProc()
    w._proc = fake
    w.stop()
    assert fake.calls == ["terminate", "wait(2)"]


def test_replay_and_playback_stop_is_event_only():
    for w in (IqReplay("/x.iq", 1, 2_000_000, on_frame=lambda *a: None),
              IqAudioPlayer("/x.iq", 2_000_000, "fm", 0, on_audio=lambda b: None)):
        w.stop()  # no _proc attribute mishap, no exception
        assert w._stop.is_set()


def test_subprocess_worker_died_vs_stopped():
    for make, mod in (
        (lambda cb: SweepStreamer(SweepConfig(), on_sweep=lambda f, p: None, on_exit=cb), sdr_mod),
        (lambda cb: Rtl433Decoder(DecodeConfig(), on_event=lambda e: None, on_exit=cb), decoders_mod),
        (lambda cb: RadioReceiver(RadioConfig(), on_audio=lambda b: None, on_exit=cb), radio_mod),
    ):
        for stopped, reason in ((False, "died"), (True, "stopped")):
            seen = []
            w = make(seen.append)
            if stopped:
                w._stop.set()
            with mock.patch.object(mod.subprocess, "Popen", return_value=FakeProc(exited=True)):
                w._run()
            assert seen == [reason], (type(w).__name__, stopped, seen)


def test_file_workers_completed_vs_stopped():
    with tempfile.NamedTemporaryFile(suffix=".iq", delete=False) as fh:
        path = fh.name
    try:
        for cls_make in (
            lambda cb: IqReplay(path, 100_000_000, 2_000_000,
                                on_frame=lambda *a: None, on_done=cb),
            lambda cb: IqAudioPlayer(path, 2_000_000, "fm", 0,
                                     on_audio=lambda b: None, on_done=cb),
        ):
            seen = []
            cls_make(seen.append)._run()
            assert seen == ["completed"], seen
            seen = []
            w = cls_make(seen.append)
            w._stop.set()
            w._run()
            assert seen == ["stopped"], seen
    finally:
        os.unlink(path)


def test_decode_file_completed_stopped_and_died():
    with tempfile.NamedTemporaryFile(suffix=".iq", delete=False) as fh:
        path = fh.name
    try:
        seen = []
        w = IqFileDecoder(path, 2_000_000, 433_920_000,
                          on_event=lambda e: None, on_done=seen.append)
        with mock.patch.object(decode_mod.subprocess, "Popen",
                               return_value=FakeProc(exited=True)):
            w._run()
        assert seen == ["completed"], seen

        seen = []
        w = IqFileDecoder(path, 2_000_000, 433_920_000,
                          on_event=lambda e: None, on_done=seen.append)
        w._stop.set()
        w._run()
        assert seen == ["stopped"], seen

        seen = []
        w = IqFileDecoder(path, 2_000_000, 433_920_000,
                          on_event=lambda e: None, on_done=seen.append)
        with mock.patch.object(decode_mod.subprocess, "Popen",
                               side_effect=FileNotFoundError("no rtl_433")):
            w._run()
        assert seen == ["died"], seen
    finally:
        os.unlink(path)


def test_adsb_died_and_tempdir_cleanup_on_stop():
    seen = []
    w = AdsbReceiver(AdsbConfig(), on_update=lambda a, m: None, on_exit=seen.append)
    with mock.patch.object(adsb_mod.subprocess, "Popen", return_value=FakeProc(exited=True)):
        w._run()
    assert seen == ["died"], seen
    tmp = w._tmp
    assert tmp is not None and Path(tmp).exists()
    w.stop()
    assert not Path(tmp).exists()
    assert w._tmp is None


def test_capture_outcome_completed_died_cancelled():
    import time as _time
    for size, stopped, reason in ((100, False, "completed"), (0, False, "died"),
                                  (0, True, "cancelled")):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            seen = []
            cap = IqCapture(CaptureConfig(duration_s=0.1, sample_rate=2_000_000),
                            on_done=lambda rec, r: seen.append(r))
            cap._path = td / "c.iq"
            cap._sidecar = td / "c.json"
            cap._path.write_bytes(b"\0" * size)
            # expected = duration*rate*2; scale to the file sizes above
            cap._expected_bytes = 100
            cap._started_at = _time.time()
            if stopped:
                cap._stop.set()
            cap._proc = FakeProc(exited=True)
            with mock.patch.object(capture_mod.subprocess, "Popen",
                                   return_value=FakeProc(exited=True)):
                # _run spawns its own proc; drive it with the fake via Popen
                cap._run()
            assert seen == [reason], (size, stopped, seen)


def test_scanner_kill_is_per_retune_not_a_stop():
    w = RadioScanner(ScanRadioConfig(freqs_mhz=[101.1]), on_audio=lambda b: None,
                     on_event=lambda n, p: None)
    fake = FakeProc(timeout_on_wait=True)
    w._proc = fake
    w._kill()
    assert fake.calls == ["terminate", "wait(2)", "kill"]
    assert not w._stop.is_set()  # retune teardown must not stop the scan


def test_autoscanner_stop_forwards_to_current_job():
    stopped = []
    class _Job:
        def stop(self):
            stopped.append(True)
    w = AutoScanner(ScanConfig(), on_event=lambda n, p: None)
    w._current_job = _Job()
    w.stop()
    assert stopped == [True]
    assert w._stop.is_set()


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
