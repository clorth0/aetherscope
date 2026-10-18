"""One home for mode-config knowledge that crosses the socket seam.

Only the demod vocabulary lives here in full. Mode field lists stay
with their dataclasses (``sdr.SweepConfig``, ``radio.RadioConfig``,
and so on, bounds settled by their constructors); the JS
``read*``/``apply*ConfigToInputs`` mirrors in ``aether-*.js`` stay
hand-written, with no build step and no code generation. The static
guard in ``tests/test_mode_config_guard.py`` is the one home for the
correspondence between the dataclass fields and those mirrors, and
it reads the demod vocabulary from here.

Named subsets, deliberately not flattened into one set:

- ``DEMODS``: every demod the receiver chain can actually produce
  (``radio.make_state``/``demodulate`` implement exactly these).
  Radio, scan-radio, and IQ playback all consume this set.
- ``BOOKMARK_DEMODS``: a bookmark may also carry no demod at all, so
  the store validates against ``DEMODS`` plus ``None``. That ``None``
  is a bookmark-only allowance, not a fourth demod.
"""

from __future__ import annotations

DEMODS: tuple[str, ...] = ("fm", "nfm", "am")

BOOKMARK_DEMODS: frozenset = frozenset({*DEMODS, None})

DEMOD_ERROR = "Invalid demod (use fm, nfm, or am)"
