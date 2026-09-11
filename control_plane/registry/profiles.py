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


#: The fields worth naming when a profile is replaced. ``node_id`` is the key
#: and never a change; everything else here is something an operator would want
#: to see move, and the order is the order it reads best in.
PROFILE_DIFF_FIELDS = (
    "device_class",
    "gpu_name",
    "gpu_count",
    "total_memory",
    "addressable_memory",
    "memory_bandwidth_gbps",
    "compute_capability",
    "driver_version",
    "hostname",
    "address",
)


def profile_diff(stored: NodeProfile | None, fresh: NodeProfile) -> dict[str, dict]:
    """What moved between two profiles, as ``{field: {"from": x, "to": y}}``.

    ``profile_supersedes`` decides whether a fresh probe should REPLACE the
    stored one. This answers the question nobody was asking afterwards: what
    actually changed. Both re-probe paths already detect a change and then log
    only ``device_class`` -- which is the one field a driver upgrade does not
    move, so a driver going 580.173.02 -> 581.0.1 logged
    ``hardware changed: gb10 -> gb10`` and recorded nothing.

    Empty when nothing moved, so a caller can use it as the trigger as well as
    the payload.
    """
    if stored is None:
        return {}
    changed: dict[str, dict] = {}
    for field in PROFILE_DIFF_FIELDS:
        before = getattr(stored, field, None)
        after = getattr(fresh, field, None)
        if before == after:
            continue
        changed[field] = {
            "from": getattr(before, "value", before),
            "to": getattr(after, "value", after),
        }
    return changed
