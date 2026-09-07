"""Which of two profiles for the same machine to believe.

A one-rule module, and the rule only became necessary when profiles started
being written on a timer. Before that there was exactly one writer -- a join --
and a join implies the far side just answered, so whatever it said was the
freshest thing anyone knew.

On a cadence that stops being true, because ``probe_local`` is **total**. It
never raises; its answer for *"I could not look"* is a fully-formed NodeProfile
that says ``UNKNOWN`` with everything zeroed. That is the right answer for the
probe to give -- it is honest about one moment -- but it means a single wedged
``nvidia-smi``, or one timed-out ``probe_remote``, produces a perfectly valid
profile asserting that a DGX Spark is unidentified hardware. Write that on a
timer and the roster flaps GB10 -> unknown -> GB10, ``_eligibility`` flips the
node in and out of the serving pool as it goes, and the operator watches a
working machine blink.

Deliberately not in ``probe.py``: the probe's job is to report what it found
this instant, and it should keep doing that without knowing what anyone
previously believed. Deliberately not in ``serde.py`` either, because the agent
re-probing its own hardware never touches a wire.
"""

from __future__ import annotations

from control_plane.contracts import DeviceClass, NodeProfile


def profile_supersedes(fresh: NodeProfile, stored: NodeProfile | None) -> bool:
    """Whether *fresh* should replace *stored*.

    The rule is one line and everything else here is why it is that line:
    **an UNKNOWN profile never replaces an identified one.**

    ``UNKNOWN`` is the only device class that does not describe hardware. Every
    other value is a positive finding -- we looked, and this is what is there.
    ``UNKNOWN`` means we could not look, and "could not look" is not evidence
    that anything changed. So it is accepted only when we had nothing better.

    Note what this deliberately does *not* do: it does not rank the identified
    classes against each other. A GB10 that comes back as CPU is believed,
    because that is what pulling a card actually looks like from here, and a
    machine whose hardware genuinely changed must be allowed to say so. Only
    the absence of an answer is filtered, never a different answer.

    The Raspberry Pi that prompted all of this gets in for free and in the
    right direction: ``unknown -> cpu`` is a machine becoming identified, which
    lands, while a transient probe failure on any healthy node never does.
    """
    if stored is None:
        return True
    if fresh.device_class is not DeviceClass.UNKNOWN:
        return True
    return stored.device_class is DeviceClass.UNKNOWN
