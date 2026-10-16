"""Socket configs refuse illegal values in the constructor."""

import os
import sys
import traceback
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.adsb import AdsbConfig  # noqa: E402
from backend.decoders import DecodeConfig  # noqa: E402
from backend.radio import RadioConfig  # noqa: E402
from backend.scan import ScanConfig  # noqa: E402
from backend.sdr import SweepConfig  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _rejects(fn):
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("constructor accepted an illegal config")


def test_defaults_construct():
    SweepConfig()
    DecodeConfig()
    RadioConfig()
    AdsbConfig()
    ScanConfig()
    AdsbConfig(rx_lat=None, rx_lon=None)
    ScanConfig(rx_lat=37.7, rx_lon=-122.4)


def test_sweep_bounds():
    _rejects(lambda: SweepConfig(f_start_mhz=-5, f_stop_mhz=99_999, bin_width_hz=1, lna_gain=999))
    _rejects(lambda: SweepConfig(f_start_mhz=100, f_stop_mhz=100))
    _rejects(lambda: SweepConfig(f_stop_mhz=6001))
    _rejects(lambda: SweepConfig(bin_width_hz=1_000))
    _rejects(lambda: SweepConfig(lna_gain=48))
    ok = SweepConfig(f_start_mhz=1, f_stop_mhz=6000, bin_width_hz=2_000, lna_gain=0)
    assert ok.vga_gain == 20


def test_decode_bounds():
    _rejects(lambda: DecodeConfig(freq_hz=-1))
    _rejects(lambda: DecodeConfig(freq_hz=10_000))
    _rejects(lambda: DecodeConfig(label="x" * 500))
    assert DecodeConfig(freq_hz=1_000_000, label="ok").label == "ok"


def test_radio_bounds_apply_on_replace():
    _rejects(lambda: RadioConfig(demod="usb", freq_mhz=1e9))
    _rejects(lambda: RadioConfig(demod="fm", freq_mhz=0.5))
    cfg = RadioConfig()
    _rejects(lambda: replace(cfg, freq_mhz=9000))
    snapped = replace(cfg, freq_mhz=101.2)
    assert snapped.freq_mhz == 101.2


def test_adsb_bounds():
    _rejects(lambda: AdsbConfig(gain_db=-100, rx_lat=999, rx_lon=999))
    _rejects(lambda: AdsbConfig(gain_db=63))
    _rejects(lambda: AdsbConfig(rx_lat=90.1))
    _rejects(lambda: AdsbConfig(rx_lon=180.1))
    assert AdsbConfig(gain_db=0, rx_lat=-90, rx_lon=180).gain_db == 0


def test_scan_bounds():
    _rejects(lambda: ScanConfig(sweep_seconds=0))
    _rejects(lambda: ScanConfig(rtl433_seconds=-1))
    _rejects(lambda: ScanConfig(adsb_seconds=float("inf")))
    _rejects(lambda: ScanConfig(rx_lat=999, rx_lon=999))
    assert ScanConfig(sweep_seconds=0.5).sweep_seconds == 0.5


def test_handlers_do_not_repeat_the_constructor_checks():
    app = (ROOT / "backend" / "app.py").read_text()
    start = app.index("def on_start_radio")
    end = app.index("def on_start_scan_radio")
    body = app[start:end]
    assert "cfg.demod not in" not in body
    assert "1.0 <= cfg.freq_mhz" not in body
    assert "capture_config_error" not in app
    assert app.count("capture_basename(") == 3
    capture = (ROOT / "backend" / "capture.py").read_text()
    assert capture.count("capture_basename(") == 3
    assert '"/" in name or ".." in name' not in app


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
