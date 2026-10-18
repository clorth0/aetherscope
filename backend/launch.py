"""Mode-launch module: one recipe for starting a mode and finishing it.

Deep module (see docs/architecture.md). Ten per-mode launch recipes in
app.py used to re-derive the same consequences behind a small army of
closures: five ``on_exit`` handlers, four hand-built ``on_done``
closures, and one scan-event closure. This module owns that recipe
behind a small interface:

    LaunchSpec   one mode as data + small callbacks: kind, how to build
                 its worker (given the finish callbacks to wire in),
                 how its finish maps to toast/socket/status, and what
                 to emit once it has started.
    Launcher     consumes one spec through one launch() path: ask the
                 host to begin the job, then emit the started actions.
                 Adding a mode is adding a spec, not a new recipe.

The seam is the ``LaunchHost`` the app supplies. JobBoard remains the
owner of job state (the host's begin/finish are its publish/finish via
app.py's ``_begin_job`` / ``_finish_job``) and WorkerLifecycle remains
the owner of start/stop/outcome() in each adapter; this module owns
only the launch recipe and the finish/toast/status derivation, in one
place. Per-mode differences that are real stay explicit in the spec's
finish mapping (capture's record payload and captures refresh,
decode_file's completed-only toast, scan's terminal events) rather
than being flattened into one finish shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

# One started/finish consequence: ("emit", name, payload) or
# ("toast", level, message). A tuple keeps the ordering byte-identical
# to the hand-built recipes this replaces.
Action = tuple

# Finish callbacks see the worker callback's own arguments: exit modes
# get (reason,), capture gets (record, reason), file modes get
# (reason,), scan gets (name, payload). In every done call the reason
# is the last argument, and any preceding arguments are `extra`.


def toast_on_completed(message: str):
    """toast_for helper: an info toast only when the run completed."""
    return lambda reason, extra, device_present: (
        ("info", message) if reason == "completed" else None)


@dataclass(frozen=True)
class FinishSpec:
    """How one mode's worker callbacks derive finish consequences.

    style is the recipe shape, one of:
      "exit": on_exit(reason) — "stopped" is silent and leaves the job
              published; any other reason finishes the job, counts a
              subprocess death, toasts "<death label> stopped
              unexpectedly." when a device is present, then emits status.
      "done": on_done(*extra, reason) — always tries to finish the job,
              emits done_event and/or the mapped toast (order set by
              toast_before_event), optionally refreshes the captures
              list, then emits status only when the job was cleared.
      "scan": on_event(name, payload) — forwards every event; only the
              terminal_events finish the job, then emit status when
              the job was cleared.
    """

    style: str
    done_event: str | None = None
    done_payload: Callable | None = None  # (extra, reason) -> dict
    toast_for: Callable | None = None     # (reason, extra, device_present) -> (level, msg) | None
    toast_before_event: bool = False
    refresh_captures: bool = False
    terminal_events: tuple = ()


def exit_finish() -> FinishSpec:
    return FinishSpec(style="exit")


def done_finish(done_event: str | None = None, done_payload: Callable | None = None,
                toast_for: Callable | None = None, toast_before_event: bool = False,
                refresh_captures: bool = False) -> FinishSpec:
    return FinishSpec(style="done", done_event=done_event, done_payload=done_payload,
                      toast_for=toast_for, toast_before_event=toast_before_event,
                      refresh_captures=refresh_captures)


def scan_finish() -> FinishSpec:
    return FinishSpec(style="scan", terminal_events=("scan_completed", "scan_failed"))


@dataclass(frozen=True)
class LaunchContext:
    """Finish callbacks a build() wires into its worker, by mode style."""

    identity: int
    on_exit: Callable
    on_done: Callable
    on_event: Callable


@dataclass(frozen=True)
class LaunchSpec:
    kind: str
    build: Callable   # (LaunchContext) -> worker (not started)
    finish: FinishSpec
    started: Callable | None = None  # (worker) -> list[Action]


class LaunchHost(Protocol):
    """The app-side seam: job board, socket, toast, status, telemetry."""

    def begin_job(self, kind: str, make: Callable): ...
    def finish_job(self, identity: int) -> bool: ...
    def emit(self, name: str, payload: dict) -> None: ...
    def toast(self, level: str, message: str) -> None: ...
    def status(self) -> None: ...
    def captures_list(self) -> None: ...
    def device_present(self) -> bool: ...
    def bump_deaths(self) -> None: ...
    def death_label(self, kind: str) -> str: ...


class Launcher:
    """The single launch path. Tests cross this same seam with a fake host."""

    def __init__(self, host: LaunchHost) -> None:
        self._host = host

    def launch(self, spec: LaunchSpec) -> bool:
        def make(identity: int):
            ctx = LaunchContext(
                identity=identity,
                on_exit=lambda reason: self._finish_exit(spec, identity, reason),
                on_done=lambda *args: self._finish_done(spec, identity, args),
                on_event=lambda name, payload: self._finish_scan(spec, identity, name, payload),
            )
            return spec.build(ctx)

        ok, worker = self._host.begin_job(spec.kind, make)
        if not ok:
            return False
        for action in (spec.started(worker) if spec.started else []):
            self._do(action)
        return True

    # -- finish derivation: the one place ------------------------------
    def _finish_exit(self, spec: LaunchSpec, identity: int, reason: str) -> None:
        if reason == "stopped":
            return
        if not self._host.finish_job(identity):
            return
        self._host.bump_deaths()
        if self._host.device_present():
            self._host.toast("error", f"{self._host.death_label(spec.kind)} stopped unexpectedly.")
        self._host.status()

    def _finish_done(self, spec: LaunchSpec, identity: int, args: tuple) -> None:
        reason = args[-1]
        extra = args[:-1]
        finish = spec.finish
        cleared = self._host.finish_job(identity)
        toast = (finish.toast_for(reason, extra, self._host.device_present())
                 if finish.toast_for else None)

        def do_toast() -> None:
            if toast is not None:
                self._host.toast(toast[0], toast[1])

        def do_event() -> None:
            if finish.done_event is not None:
                payload = finish.done_payload(extra, reason) if finish.done_payload else {}
                self._host.emit(finish.done_event, payload)

        if finish.toast_before_event:
            do_toast(); do_event()
        else:
            do_event(); do_toast()
        if finish.refresh_captures:
            self._host.captures_list()
        if cleared:
            self._host.status()

    def _finish_scan(self, spec: LaunchSpec, identity: int, name: str, payload: dict) -> None:
        if name not in spec.finish.terminal_events:
            self._host.emit(name, payload)
            return
        cleared = self._host.finish_job(identity)
        self._host.emit(name, payload)
        if cleared:
            self._host.status()

    def _do(self, action: Action) -> None:
        if action[0] == "emit":
            self._host.emit(action[1], action[2])
        elif action[0] == "toast":
            self._host.toast(action[1], action[2])
        else:  # pragma: no cover - a spec bug, not a runtime mode
            raise ValueError(f"unknown launch action {action[0]!r}")
