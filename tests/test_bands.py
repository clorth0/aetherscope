"""Tests for band classification (backend.scan.classify_band)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.scan import classify_band  # noqa: E402


def test_uhf_business_band_labeled():
    # 459.5 MHz sits in the US UHF land-mobile / business band (450-470),
    # which used to fall through to "Unallocated / Unknown".
    label, _decoder = classify_band(459_500_000)
    assert label == "UHF Business / Land Mobile", f"got {label!r}"


def test_known_bands_still_classify():
    assert classify_band(98_500_000)[0] == "FM Broadcast"
    assert classify_band(433_920_000)[0] == "ISM 433"      # nested before 70cm Ham
    assert classify_band(145_000_000)[0] == "2m Ham"
    assert classify_band(1_090_000_000)[0] == "ADS-B 1090"


def test_gmrs_frs_nested_inside_uhf_business():
    # GMRS/FRS (462.55-467.725) is a narrower band nested in UHF Business
    # (450-470); the narrower label must win.
    assert classify_band(462_562_500)[0] == "GMRS / FRS"   # FRS ch1 / GMRS
    assert classify_band(467_587_500)[0] == "GMRS / FRS"   # FRS ch8 (467.x)
    # a UHF business frequency outside GMRS still resolves to the broad band
    assert classify_band(459_500_000)[0] == "UHF Business / Land Mobile"


def test_true_gap_still_unknown():
    # 70 MHz is below the lowest mapped band -> genuinely unmapped.
    assert classify_band(70_000_000)[0] == "Unallocated / Unknown"


if __name__ == "__main__":
    import traceback

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
