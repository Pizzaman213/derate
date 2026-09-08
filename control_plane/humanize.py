"""Byte counts, as a person reads them.

One function, because the alternative was three. ``fit/calculator.py`` had the
careful version -- it steps down through MiB and KiB so a near miss cannot
render as "over budget by 0.0 GiB" -- and ``gateway/internal_api.py`` had a
one-line divide that reproduced exactly that bug in the download strings, where
"stopped after 0.0 GiB of 4.2 GiB" is what an early failure looked like.

``planner/comm.py::human_bytes`` is deliberately **not** folded in here. It
formats per-step transfer volumes that sit in the same sentence as a measured
link bandwidth, and bandwidth is quoted in decimal GB/s throughout the planner
because that is the unit the measurement comes back in. Binary volumes beside
decimal rates would be a worse sentence, not a more consistent one. Two
formatters is right when there are genuinely two units; the mistake was three
formatters for two units.
"""

from __future__ import annotations

KIB = 1024
MIB = 1024**2
GIB = 1024**3


def binary_bytes(n: float) -> str:
    """A byte count, in the largest binary unit that does not round it away.

    One decimal place of GiB is right for almost everything and wrong for the
    case that matters most: a refusal whose overage is small. A model that
    misses by 40 MiB rendered as "Over budget by 0.0 GiB" -- a sentence that
    says the thing does not fit and that it is over by nothing, in the same
    breath. That is the one string a person is reading when they most need to
    trust it, and it reads as a broken calculation rather than as a near miss.

    So a quantity that is genuinely zero still prints "0.0 GiB" -- an empty
    comm buffer is a real zero and should look like one -- while anything
    non-zero steps down through MiB and KiB until it has a digit to show.
    """
    if n <= 0:
        return f"{n / GIB:.1f} GiB"
    if n >= 0.05 * GIB:
        return f"{n / GIB:.1f} GiB"
    if n >= MIB:
        return f"{n / MIB:.0f} MiB"
    if n >= KIB:
        return f"{n / KIB:.0f} KiB"
    return "1 byte" if round(n) == 1 else f"{n:.0f} bytes"
