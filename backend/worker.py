"""One worker lifecycle for every mode.

A worker is a daemon thread, a stop event, and — for the hardware
modes — one subprocess. That shape was previously re-derived in ten
classes; this module owns it behind a small interface:

    start() -> bool     False when already running (the start-guard)
    stop() -> None      set the event, terminate -> wait -> kill, cleanup
    is_running() -> bool

Each mode module is an adapter at this seam: it supplies only its
command, its parsing, its callbacks, and — through ``outcome()`` — its
mode-specific natural exit (subprocess modes die, file modes complete).
Exit-reason mapping lives in ``outcome()`` alone so a mode cannot drift
its completed/died timing away from the others: callers evaluate their
completion condition at exactly the point they always did, and this
module turns (stopped?, completed?) into the reason string.

Per-adapter differences that are intentional stay explicit at the seam:
the subprocess wait timeout (ADS-B waits 3 s, the rest 2 s), the stopped
reason (capture reports "cancelled"), and cleanup on stop (ADS-B removes
its tempdir, auto-scan halts its current phase worker) via the
``_cleanup_on_stop`` hook. ``_prepare_start`` runs after the guard, so a
refused start has no side effects (capture does not rewrite its sidecar).
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import Callable

log = logging.getLogger(__name__)


class WorkerLifecycle:
    """Thread + stop event + optional subprocess, for one job run."""

    def __init__(self, wait_timeout: float = 2) -> None:
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wait_timeout = wait_timeout

    # -- the interface -------------------------------------------------
    def start(self) -> bool:
        """Start the worker thread; refuse (False) while already running."""
        if self._thread and self._thread.is_alive():
            return False
        self._prepare_start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop: flag the event, tear the subprocess down, then clean up."""
        self._stop.set()
        self._terminate_proc()
        self._cleanup_on_stop()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- outcome mapping (the one place) --------------------------------
    def outcome(self, *, completed: bool, stopped_reason: str = "stopped",
                completed_reason: str = "completed",
                died_reason: str = "died") -> str:
        """Map exit state to a reason, preserving each mode's timing.

        The caller decides ``completed`` at its own end-of-run point
        (subprocess modes pass False, file modes True, capture passes its
        file-size check); this module alone decides the string.
        """
        if self._stop.is_set():
            return stopped_reason
        return completed_reason if completed else died_reason

    def emit_exit(self, on_exit: Callable[[str], None] | None, *,
                  completed: bool, stopped_reason: str = "stopped",
                  completed_reason: str = "completed",
                  died_reason: str = "died") -> None:
        """Call ``on_exit`` with ``outcome(...)``, logging callback failure."""
        if not on_exit:
            return
        reason = self.outcome(completed=completed,
                              stopped_reason=stopped_reason,
                              completed_reason=completed_reason,
                              died_reason=died_reason)
        try:
            on_exit(reason)
        except Exception:
            log.exception("on_exit callback failed")

    # -- internals shared by adapters -----------------------------------
    def _terminate_proc(self) -> None:
        """terminate -> wait(timeout) -> kill, for the current subprocess."""
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=self._wait_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()

    # -- adapter hooks ---------------------------------------------------
    def _prepare_start(self) -> None:
        """Run after the start-guard, before the thread spawns."""

    def _cleanup_on_stop(self) -> None:
        """Run at the end of stop(), after subprocess teardown."""

    def _run(self) -> None:  # pragma: no cover - each adapter supplies this
        raise NotImplementedError
