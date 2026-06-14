"""Tests for hackrf_sweep output assembly (backend.sdr.assemble_sweeps).

hackrf_sweep emits the tuning segments of each sweep in a NON-monotonic
order (e.g. 88 -> 98 -> 93 -> 103 MHz) and repeats the same segment set
every cycle. assemble_sweeps must emit exactly one complete, frequency-
sorted row per full sweep cycle -- not one row per out-of-order segment.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.sdr import assemble_sweeps  # noqa: E402


def _seg(hz_low, bin_width, powers):
    """Build one hackrf_sweep CSV line."""
    hz_high = int(hz_low + bin_width * len(powers))
    fields = [
        "2026-01-01", "00:00:00.000000",
        str(hz_low), str(hz_high), str(bin_width), "204",
        *[str(p) for p in powers],
    ]
    return ", ".join(fields)


def test_one_row_per_cycle_with_out_of_order_segments():
    # FM-like span 88-108 MHz, 1 MHz bins, 4 segments of 5 bins each,
    # emitted OUT OF ORDER exactly as hackrf_sweep does.
    bw = 1_000_000
    order = [88, 98, 93, 103]  # MHz, non-monotonic
    lines = []
    for _cycle in range(3):
        for mhz in order:
            base = mhz * 1_000_000
            powers = [float(mhz + b) for b in range(5)]  # bin power == its MHz
            lines.append(_seg(base, bw, powers))

    rows = list(assemble_sweeps(lines))

    # The current cycle isn't flushed until the next one begins, so 3 fed
    # cycles yield 2 complete rows.
    assert len(rows) == 2, f"expected 2 complete rows, got {len(rows)}"

    for freqs, powers in rows:
        assert len(freqs) == 20, f"expected 20 bins, got {len(freqs)}"
        assert freqs[0] == 88_000_000
        assert freqs[-1] == 107_000_000
        assert list(freqs) == sorted(freqs), "frequencies must be ascending"
        assert len(powers) == len(freqs)
        # power mapping preserved through the reordering
        idx = list(freqs).index(93_000_000)
        assert powers[idx] == 93.0


def test_ignores_malformed_lines():
    bw = 1_000_000
    lines = [
        "garbage line",
        "",
        _seg(88_000_000, bw, [1.0, 2.0]),
        "2026, 00:00, notanumber, x, y, z, 1.0",  # 7 fields but unparseable
        _seg(90_000_000, bw, [3.0, 4.0]),
        _seg(88_000_000, bw, [1.0, 2.0]),  # cycle restart -> flush row 1
        _seg(90_000_000, bw, [3.0, 4.0]),
        _seg(88_000_000, bw, [1.0, 2.0]),  # cycle restart -> flush row 2
    ]
    rows = list(assemble_sweeps(lines))
    assert len(rows) == 2
    freqs, _powers = rows[0]
    assert list(freqs) == [88_000_000, 89_000_000, 90_000_000, 91_000_000]


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
