"""ADS-B enrichment: US registration (N-number) and country from an ICAO hex.

Both are derived from the 24-bit ICAO address with no external database. The
N-number algorithm is the standard FAA mapping for US addresses
(0xA00001..0xADF7C7); buckets sum to the exact block size (101711*9 = 915399)
and the endpoints check out (A00001 -> N1, ADF7C7 -> N99999).
"""

from __future__ import annotations

_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ"   # 24 letters, no I or O
_US_START = 0xA00001
_US_END = 0xADF7C7
_B1, _B2, _B3, _B4 = 101711, 10111, 951, 35


def _icao_int(hex_str):
    try:
        return int(hex_str, 16)
    except (TypeError, ValueError):
        return None


def _suffix(rem: int) -> str:
    """rem in 0..600 -> '' | one letter | two letters (24-letter set)."""
    if rem == 0:
        return ""
    rem -= 1
    if rem < 24:
        return _ALPHA[rem]
    rem -= 24
    return _ALPHA[rem // 24] + _ALPHA[rem % 24]


def icao_to_registration(hex_str) -> str | None:
    """US N-number for a US ICAO address, else None."""
    icao = _icao_int(hex_str)
    if icao is None or not (_US_START <= icao <= _US_END):
        return None
    offset = icao - _US_START
    out = "N"

    d1, rem = divmod(offset, _B1)
    out += str(d1 + 1)               # leading digit 1-9
    if rem < 601:
        return out + _suffix(rem)
    rem -= 601

    d2, rem = divmod(rem, _B2)
    out += str(d2)
    if rem < 601:
        return out + _suffix(rem)
    rem -= 601

    d3, rem = divmod(rem, _B3)
    out += str(d3)
    if rem < 601:
        return out + _suffix(rem)
    rem -= 601

    d4, rem = divmod(rem, _B4)
    out += str(d4)
    if rem == 0:                     # bucket4: 0 -> '', 1-24 -> letter, 25-34 -> digit
        return out
    if rem <= 24:
        return out + _ALPHA[rem - 1]
    return out + str(rem - 25)


# ICAO 24-bit address country blocks (common North American + European traffic
# subset; (start, end, name), inclusive). Not exhaustive.
_COUNTRY_BLOCKS = [
    (0x0D0000, 0x0D7FFF, "Mexico"),
    (0x100000, 0x1FFFFF, "Russia"),
    (0x300000, 0x33FFFF, "Italy"),
    (0x340000, 0x37FFFF, "Spain"),
    (0x380000, 0x3BFFFF, "France"),
    (0x3C0000, 0x3FFFFF, "Germany"),
    (0x400000, 0x43FFFF, "United Kingdom"),
    (0x440000, 0x447FFF, "Austria"),
    (0x448000, 0x44FFFF, "Belgium"),
    (0x460000, 0x467FFF, "Finland"),
    (0x468000, 0x46FFFF, "Greece"),
    (0x478000, 0x47FFFF, "Norway"),
    (0x480000, 0x487FFF, "Netherlands"),
    (0x490000, 0x497FFF, "Poland"),
    (0x4A0000, 0x4A7FFF, "Sweden"),
    (0x4B0000, 0x4B7FFF, "Switzerland"),
    (0x4C0000, 0x4C7FFF, "Portugal"),
    (0x500000, 0x5003FF, "San Marino"),
    (0x780000, 0x7BFFFF, "China"),
    (0x7C0000, 0x7FFFFF, "Australia"),
    (0x800000, 0x83FFFF, "India"),
    (0x840000, 0x87FFFF, "Japan"),
    (0xA00000, 0xAFFFFF, "United States"),
    (0xC00000, 0xC3FFFF, "Canada"),
    (0xC80000, 0xC87FFF, "New Zealand"),
    (0xE40000, 0xE7FFFF, "Brazil"),
    (0xE80000, 0xE80FFF, "Chile"),
]


def icao_country(hex_str) -> str | None:
    icao = _icao_int(hex_str)
    if icao is None:
        return None
    for start, end, name in _COUNTRY_BLOCKS:
        if start <= icao <= end:
            return name
    return None
