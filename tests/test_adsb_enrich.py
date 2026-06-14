"""Tests for ADS-B enrichment: N-number + country. Hardware-free."""

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.adsb_enrich import icao_country, icao_to_registration  # noqa: E402


def test_nnumber_anchors():
    assert icao_to_registration("a00001") == "N1"        # first US allocation
    assert icao_to_registration("adf7c7") == "N99999"    # last US allocation


def test_nnumber_suffix_progression():
    assert icao_to_registration("a00002") == "N1A"
    assert icao_to_registration("a00019") == "N1Z"
    assert icao_to_registration("a0001a") == "N1AA"
    assert icao_to_registration("a0025a") == "N10"       # offset 601 rolls to N10


def test_nnumber_accepts_uppercase():
    assert icao_to_registration("A00001") == "N1"


def test_nnumber_non_us_is_none():
    assert icao_to_registration("400000") is None        # UK block
    assert icao_to_registration("c00001") is None        # Canada block
    assert icao_to_registration("000000") is None


def test_country_lookup():
    assert icao_country("a74fa2") == "United States"
    assert icao_country("c00001") == "Canada"
    assert icao_country("400000") == "United Kingdom"
    assert icao_country("3c0001") == "Germany"
    assert icao_country("000001") is None                # unallocated/unknown


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
