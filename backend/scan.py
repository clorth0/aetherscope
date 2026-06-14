"""Auto-scan orchestrator.

Multi-phase signal survey for security RX workflows:
  1. Wideband sweep (88-1000 MHz) -> peak detection
  2. rtl_433 on 433.92 MHz       -> ISM device IDs
  3. rtl_433 on 915 MHz          -> ISM device IDs
  4. readsb-hackrf at 1090 MHz   -> ADS-B aircraft

Each phase reuses the existing subprocess wrappers but runs them
sequentially so they don't fight over the HackRF.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .adsb import AdsbConfig, AdsbReceiver
from .decoders import DecodeConfig, Rtl433Decoder
from .sdr import SweepConfig, SweepStreamer

log = logging.getLogger(__name__)

EventCallback = Callable[[str, dict], None]


# ------------------------------------------------------------------
# Band classification
# ------------------------------------------------------------------
BAND_MAP = [
    # Order matters: list narrower/more-specific bands first when they
    # nest inside a wider one (e.g., ISM 433 sits inside 70cm Ham).
    # (start_hz, end_hz, label, suggested_decoder)
    (    88_000_000,   108_000_000, "FM Broadcast",          None),
    (   108_000_000,   118_000_000, "Aviation Nav",          None),
    (   118_000_000,   137_000_000, "Aircraft AM (VHF)",     None),
    (   137_000_000,   138_000_000, "NOAA APT Sat",          None),
    (   144_000_000,   148_000_000, "2m Ham",                None),
    (   162_400_000,   162_600_000, "NOAA Weather",          None),
    (   174_000_000,   216_000_000, "VHF TV",                None),
    (   225_000_000,   400_000_000, "Military Air",          None),
    (   433_050_000,   434_790_000, "ISM 433",               "rtl_433"),
    (   420_000_000,   450_000_000, "70cm Ham",              None),
    (   462_550_000,   467_725_000, "GMRS / FRS",            None),
    (   450_000_000,   470_000_000, "UHF Business / Land Mobile", None),
    (   470_000_000,   700_000_000, "UHF TV",                None),
    (   700_000_000,   805_000_000, "Cellular LTE B12/B13",  None),
    (   850_000_000,   894_000_000, "Cellular LTE B5",       None),
    (   902_000_000,   928_000_000, "ISM 915",               "rtl_433"),
    ( 1_080_000_000, 1_100_000_000, "ADS-B 1090",            "adsb"),
    ( 1_525_000_000, 1_559_000_000, "Inmarsat / GPS L1",     None),
    ( 1_710_000_000, 1_980_000_000, "Cellular LTE B1/B3",    None),
    ( 2_400_000_000, 2_483_500_000, "Wi-Fi 2.4 / BT / ISM",  None),
    ( 5_170_000_000, 5_835_000_000, "Wi-Fi 5",               None),
]


def classify_band(hz: float) -> tuple[str, str | None]:
    for start, end, label, decoder in BAND_MAP:
        if start <= hz <= end:
            return label, decoder
    return "Unallocated / Unknown", None


# ------------------------------------------------------------------
# Phase configuration
# ------------------------------------------------------------------
@dataclass
class ScanConfig:
    sweep_seconds: float = 12.0
    rtl433_seconds: float = 20.0
    adsb_seconds: float = 20.0
    peak_threshold_db: float = 15.0   # above noise floor
    rx_lat: float | None = None
    rx_lon: float | None = None


@dataclass
class PhaseSummary:
    name: str
    label: str
    started_at: float
    finished_at: float | None = None
    findings: dict = field(default_factory=dict)


# ------------------------------------------------------------------
# Auto-scanner
# ------------------------------------------------------------------
class AutoScanner:
    def __init__(self, config: ScanConfig, on_event: EventCallback):
        self.config = config
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current_job: Any = None
        self._phases: list[PhaseSummary] = []

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._current_job:
            try:
                self._current_job.stop()
            except Exception:
                pass
        self._current_job = None

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _emit(self, name: str, payload: dict) -> None:
        try:
            self.on_event(name, payload)
        except Exception:
            log.exception("on_event failed")

    def _run(self) -> None:
        scan_started_at = time.time()
        self._emit("scan_started", {"started_at": scan_started_at, "config": self.config.__dict__})

        try:
            peaks = self._phase_sweep()
            if self._stop.is_set(): return
            ism_433_events = self._phase_rtl433(433_920_000, "ISM 433 MHz", "rtl433_433")
            if self._stop.is_set(): return
            ism_915_events = self._phase_rtl433(915_000_000, "ISM 915 MHz", "rtl433_915")
            if self._stop.is_set(): return
            aircraft = self._phase_adsb()
            if self._stop.is_set(): return

            self._emit("scan_completed", {
                "started_at": scan_started_at,
                "finished_at": time.time(),
                "phases": [p.__dict__ for p in self._phases],
                "summary": {
                    "peak_count": len(peaks),
                    "ism_433_devices": len(ism_433_events),
                    "ism_915_devices": len(ism_915_events),
                    "aircraft_count": len(aircraft),
                },
            })
        except Exception:
            log.exception("auto-scan failed")
            self._emit("scan_failed", {"reason": "exception"})

    # -------------------- Phase 1: wideband sweep --------------------
    def _phase_sweep(self) -> list[dict]:
        phase = PhaseSummary(name="sweep", label="Wideband sweep 88-1000 MHz",
                             started_at=time.time())
        self._phases.append(phase)
        self._emit("phase_started", {"phase": phase.name, "label": phase.label,
                                     "duration_s": self.config.sweep_seconds})

        max_powers: dict[int, float] = defaultdict(lambda: -200.0)

        def on_row(freqs: np.ndarray, powers: np.ndarray):
            for f, p in zip(freqs, powers):
                key = int(f // 500_000) * 500_000
                if p > max_powers[key]:
                    max_powers[key] = float(p)

        streamer = SweepStreamer(
            SweepConfig(f_start_mhz=88, f_stop_mhz=1000, bin_width_hz=500_000,
                        lna_gain=16, vga_gain=20, amp_enable=False),
            on_sweep=on_row,
        )
        self._current_job = streamer
        streamer.start()
        self._sleep_with_progress(phase.name, self.config.sweep_seconds)
        streamer.stop()
        self._current_job = None

        peaks = self._extract_peaks(dict(max_powers))
        phase.findings["peaks"] = peaks
        phase.finished_at = time.time()
        self._emit("phase_completed", {"phase": phase.name, "findings": phase.findings})
        return peaks

    def _extract_peaks(self, max_powers: dict[int, float]) -> list[dict]:
        if not max_powers:
            return []
        values = list(max_powers.values())
        noise_floor = float(np.median(values))
        threshold = noise_floor + self.config.peak_threshold_db

        signal_bins = [(f, p) for f, p in sorted(max_powers.items()) if p > threshold]
        if not signal_bins:
            return []

        # Group adjacent bins (within 2 MHz) into single peaks
        groups: list[dict] = []
        for f, p in signal_bins:
            if groups and f - groups[-1]["end_hz"] <= 2_000_000:
                groups[-1]["end_hz"] = f
                groups[-1]["peak_db"] = max(groups[-1]["peak_db"], p)
            else:
                groups.append({"start_hz": f, "end_hz": f, "peak_db": p})

        # Annotate each with band classification
        for g in groups:
            center = (g["start_hz"] + g["end_hz"]) / 2
            label, decoder = classify_band(center)
            g["center_hz"] = int(center)
            g["band"] = label
            g["decoder_hint"] = decoder
            g["snr_db"] = round(g["peak_db"] - noise_floor, 1)

        groups.sort(key=lambda g: g["snr_db"], reverse=True)
        return groups

    # -------------------- Phase 2/3: rtl_433 -------------------------
    def _phase_rtl433(self, freq_hz: int, label: str, name: str) -> list[dict]:
        phase = PhaseSummary(name=name, label=f"rtl_433 {label}", started_at=time.time())
        self._phases.append(phase)
        self._emit("phase_started", {"phase": name, "label": phase.label,
                                     "duration_s": self.config.rtl433_seconds})

        seen: dict[str, dict] = {}

        def on_event(ev: dict):
            key = f"{ev.get('model', '?')}/{ev.get('id', ev.get('hex', ev.get('channel', '?')))}"
            if key not in seen:
                seen[key] = {"first": ev, "count": 0, "last_time": ev.get("time")}
            seen[key]["count"] += 1
            seen[key]["last_time"] = ev.get("time")
            # also live-emit the raw event
            self._emit("scan_decoded", {"phase": name, "event": ev})

        decoder = Rtl433Decoder(
            DecodeConfig(freq_hz=freq_hz, sample_rate=2_400_000, gain_db=32,
                         label=label),
            on_event=on_event,
        )
        self._current_job = decoder
        decoder.start()
        self._sleep_with_progress(name, self.config.rtl433_seconds)
        decoder.stop()
        self._current_job = None

        # Build deduped device list
        devices: list[dict] = []
        for key, info in seen.items():
            d = dict(info["first"])
            d["_count"] = info["count"]
            d["_last_time"] = info["last_time"]
            d["_key"] = key
            devices.append(d)
        phase.findings["devices"] = devices
        phase.finished_at = time.time()
        self._emit("phase_completed", {"phase": name, "findings": phase.findings})
        return devices

    # -------------------- Phase 4: ADS-B -----------------------------
    def _phase_adsb(self) -> list[dict]:
        phase = PhaseSummary(name="adsb", label="ADS-B 1090 MHz", started_at=time.time())
        self._phases.append(phase)
        self._emit("phase_started", {"phase": phase.name, "label": phase.label,
                                     "duration_s": self.config.adsb_seconds})

        seen: dict[str, dict] = {}

        def on_update(aircraft: list[dict], meta: dict):
            for a in aircraft:
                hex_id = a.get("hex")
                if not hex_id:
                    continue
                seen[hex_id] = a

        recv = AdsbReceiver(
            AdsbConfig(gain_db=32, rx_lat=self.config.rx_lat, rx_lon=self.config.rx_lon),
            on_update=on_update,
        )
        self._current_job = recv
        recv.start()
        self._sleep_with_progress(phase.name, self.config.adsb_seconds)
        recv.stop()
        self._current_job = None

        aircraft_list = list(seen.values())
        phase.findings["aircraft"] = aircraft_list
        phase.finished_at = time.time()
        self._emit("phase_completed", {"phase": phase.name, "findings": phase.findings})
        return aircraft_list

    # -------------------- helpers ------------------------------------
    def _sleep_with_progress(self, phase: str, duration_s: float) -> None:
        # Emit per-half-second progress so the UI's bar can animate, but
        # interrupt instantly when stop() fires.
        start = time.time()
        while True:
            elapsed = time.time() - start
            if elapsed >= duration_s:
                return
            self._emit("phase_progress", {
                "phase": phase,
                "elapsed_s": round(elapsed, 1),
                "duration_s": duration_s,
                "pct": round(min(100.0, elapsed / duration_s * 100), 1),
            })
            if self._stop.wait(0.5):
                return
