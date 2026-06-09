"""HackRF device probe via `hackrf_info`."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

log = logging.getLogger(__name__)

HACKRF_INFO = shutil.which("hackrf_info") or "/opt/homebrew/bin/hackrf_info"

_SERIAL_RE   = re.compile(r"^\s*Serial number:\s*(\S+)\s*$")
_BOARD_RE    = re.compile(r"^\s*Board ID Number:\s*\d+\s*\((.+)\)\s*$")
_FIRMWARE_RE = re.compile(r"^\s*Firmware Version:\s*(\S+)")


def probe_hackrf(timeout: float = 2.0) -> dict | None:
    """Run hackrf_info; return device info dict or None if no device.

    Returned shape: {"serial": str, "board": str, "firmware": str}
    """
    try:
        result = subprocess.run(
            [HACKRF_INFO],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    text = (result.stdout or "") + (result.stderr or "")
    if "No HackRF boards found" in text or "Found HackRF" not in text:
        return None

    info: dict = {}
    for line in text.splitlines():
        if m := _SERIAL_RE.match(line):
            info["serial"] = m.group(1)
        elif m := _BOARD_RE.match(line):
            info["board"] = m.group(1)
        elif m := _FIRMWARE_RE.match(line):
            info["firmware"] = m.group(1)
    return info or None
