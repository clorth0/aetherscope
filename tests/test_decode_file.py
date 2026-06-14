"""Tests for cs8 -> cu8 conversion used by file decoding. Hardware-free."""

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from backend.decode_file import to_cu8  # noqa: E402


def test_to_cu8_center_and_extremes():
    # complex float (-1..1) -> interleaved unsigned 8-bit centered at 128.
    assert list(to_cu8(np.array([0 + 0j], dtype=np.complex64))) == [128, 128]
    assert list(to_cu8(np.array([1 + 0j], dtype=np.complex64))) == [255, 128]
    assert list(to_cu8(np.array([-1 + 0j], dtype=np.complex64))) == [0, 128]
    assert list(to_cu8(np.array([0 + 1j], dtype=np.complex64))) == [128, 255]


def test_to_cu8_length_is_two_per_sample():
    assert len(to_cu8(np.zeros(50, dtype=np.complex64))) == 100


def test_to_cu8_clips():
    # Values beyond full scale clip into 0..255, never wrap.
    b = to_cu8(np.array([2 + 0j, -2 + 0j], dtype=np.complex64))
    assert b[0] == 255 and b[2] == 0


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
