#!/usr/bin/env python3
"""PROTOTYPE. Not imported by the app.

Show that SweepConfig, DecodeConfig, RadioConfig, and AdsbConfig accept
values the radio path already rejects, and that CaptureConfig can be
built when capture_config_error would refuse it. A constructor check
closes that, and the socket handlers already catch ValueError.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from backend.adsb import AdsbConfig
from backend.capture import CaptureConfig, capture_config_error
from backend.decoders import DecodeConfig
from backend.radio import RadioConfig
from backend.sdr import SweepConfig


def require_sweep(cfg: SweepConfig) -> None:
    if not (1 <= cfg.f_start_mhz < cfg.f_stop_mhz <= 6000):
        raise ValueError(f"sweep range must sit inside 1-6000 MHz, got {cfg.f_start_mhz}-{cfg.f_stop_mhz}")
    if cfg.bin_width_hz < 2_000 or cfg.bin_width_hz > 5_000_000:
        raise ValueError(f"bin width out of range: {cfg.bin_width_hz}")
    if not (0 <= cfg.lna_gain <= 40):
        raise ValueError(f"lna gain out of range: {cfg.lna_gain}")


def main() -> int:
    illegal = SweepConfig(f_start_mhz=-5, f_stop_mhz=99_999, bin_width_hz=1, lna_gain=999)
    print(f"SweepConfig constructed: {illegal}")
    try:
        require_sweep(illegal)
    except ValueError as exc:
        print(f"constructor check rejected it: {exc}")
    else:
        print("constructor check did not reject the illegal sweep")
        return 1

    others = [
        DecodeConfig(freq_hz=-1, sample_rate=1, label="x" * 500),
        RadioConfig(demod="usb", freq_mhz=1e9),
        AdsbConfig(gain_db=-100, rx_lat=999, rx_lon=999),
        CaptureConfig(duration_s=1e9, label="<script>"),
    ]
    for cfg in others:
        extra = ""
        if isinstance(cfg, CaptureConfig):
            extra = f" capture_config_error={capture_config_error(cfg)!r}"
        print(f"constructed {type(cfg).__name__}{extra}")

    @dataclass
    class CheckedSweep:
        f_start_mhz: int
        f_stop_mhz: int
        bin_width_hz: int
        lna_gain: int

        def __post_init__(self) -> None:
            require_sweep(
                SweepConfig(
                    f_start_mhz=self.f_start_mhz,
                    f_stop_mhz=self.f_stop_mhz,
                    bin_width_hz=self.bin_width_hz,
                    lna_gain=self.lna_gain,
                )
            )

    try:
        CheckedSweep(f_start_mhz=-5, f_stop_mhz=99_999, bin_width_hz=1, lna_gain=999)
    except ValueError:
        print("a dataclass __post_init__ can refuse the same value at construction")
        return 0
    print("post_init sketch failed to refuse")
    return 1


if __name__ == "__main__":
    sys.exit(main())
