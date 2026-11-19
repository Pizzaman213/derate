"""The usage cycle: 30 days, anchored at the key's or org's creation.

There is no calendar month here on purpose. A key created on the 31st has no
"same day next month", and a cycle that is a fixed number of seconds long
makes ``resets_at`` a sum rather than a timezone question.
"""

from __future__ import annotations

import math

from control_plane.contracts.auth import CYCLE_DAYS

CYCLE_S = CYCLE_DAYS * 86400


def cycle_bounds(anchor: float, now: float) -> tuple[float, float]:
    """``(cycle_start, resets_at)`` of the cycle ``now`` falls in.

    ``n = floor((now - anchor) / CYCLE_S)``, never below 0 -- a ``now`` before
    the anchor is the first cycle, not cycle -1. ``cycle_start`` is a counter
    row's key, so it must come out bit-identical for every ``now`` inside one
    cycle: it is always ``anchor + n * CYCLE_S``, and the two corrections only
    move ``n`` when float division landed on the wrong side of a boundary
    (``now == resets_at`` is the next cycle's first instant).
    """
    n = max(0, math.floor((now - anchor) / CYCLE_S))
    while n > 0 and now < anchor + n * CYCLE_S:
        n -= 1
    while now >= anchor + (n + 1) * CYCLE_S:
        n += 1
    start = anchor + n * CYCLE_S
    return start, anchor + (n + 1) * CYCLE_S
