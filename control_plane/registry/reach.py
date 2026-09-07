"""Can these two machines actually talk to each other?

This is not the link measurement. `links/measure.py` saturates the fabric for
about a minute to find out how *fast* a pair is; this asks the far cheaper and
far more often relevant question -- whether a packet gets there at all, from
which side, and what the failure says when it does not. An operator staring at
a node that joined and then went quiet needs that answer in a second, not in a
minute, and needs it before a bandwidth figure means anything.

The distinction the ladder in `links/measure.py` draws between a real number
and an estimate, this module draws between a real answer and a timeout: a leg
that did not answer carries `ok: false` and the transport's own error string,
never a zero millisecond figure that would plot as a fast link.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

#: One dial. Short on purpose: this runs while a human waits, and a node that
#: needs longer than this to answer a health check is a finding, not a slow
#: network. The coordinator's own heartbeat uses 2.0s for the same call.
REACH_TIMEOUT_S = 3.0

#: The outer bound on "ask node A to dial node B". Must exceed REACH_TIMEOUT_S:
#: A spends up to that dialling before it can answer us at all, and a bound
#: below it would report A as unreachable every time B is.
PEER_REACH_TIMEOUT_S = REACH_TIMEOUT_S + 3.0

#: Where the check is made from, when it is not another node.
COORDINATOR = "coordinator"


class UnusableTarget(ValueError):
    """The address to dial is not one this agent will accept."""


@dataclass(frozen=True)
class ReachLeg:
    """One direction of one pair. Legs are directional and reported separately.

    Asymmetry is the interesting case, not an inconsistency to be averaged
    away: a worker behind a NAT reaches the coordinator while the coordinator
    cannot reach it back, and a single merged "connected: false" would hide
    which half is broken -- which is the half that tells you what to fix.
    """

    source: str
    """node_id of the machine that dialled, or "coordinator"."""

    target: str
    """node_id of the machine dialled."""

    url: str
    """Exactly what was dialled, so an operator can try it themselves."""

    ok: bool
    ms: float | None = None
    """Round trip in milliseconds. None whenever ok is false -- never 0."""

    error: str | None = None
    answered_as: str | None = None
    """The node_id the far side called itself. A mismatch is its own fault."""

    pair: bool = True
    """True when this leg tests a direction BETWEEN the two nodes asked about.

    False for the coordinator's own probe of an endpoint when the coordinator
    is neither of them: reaching two machines from a third says nothing about
    whether those two can reach each other, and counting it as if it did is how
    "reachable both ways" ends up printed under two untested directions."""

    note: str | None = None
    """Set on a leg that was not dialled, saying why. Two kinds, told apart by
    `ok`: the coordinator's leg to itself (ok, nothing to dial) and a direction
    that could not be tested at all (not ok, and not a failure either -- see
    `unknown_leg`)."""

    def as_dict(self) -> dict:
        return asdict(self)


def self_leg(node_id: str, url: str) -> ReachLeg:
    """The coordinator's leg to itself. Not dialled, and not silently omitted.

    Leaving it out would make a two-node check look like it ran one probe when
    it ran two questions; saying "this is the process you are asking" is the
    honest form of an answer nobody had to go over the wire for.
    """
    return ReachLeg(
        source=COORDINATOR,
        target=node_id,
        url=url,
        ok=True,
        pair=False,
        note="this is the process answering, so nothing was dialled",
    )


def unknown_leg(
    source: str, target: str, url: str, why: str, error: str, pair: bool = True
) -> ReachLeg:
    """A direction that could not be tested. Not the same as one that failed.

    If we cannot reach node A to ask it anything, whether A can reach B is
    unknown, and reporting it as "A cannot reach B" invents a result -- one
    that would send an operator to look at a cable that is fine. It is equally
    not a pass, so `ok` is false and the verdict says the direction was never
    checked rather than counting it as reachable.
    """
    return ReachLeg(
        source=source, target=target, url=url, ok=False, error=error, note=why, pair=pair
    )


def validate_target(url: str) -> str:
    """Accept an http(s) URL with a host. Raise UnusableTarget otherwise.

    An agent will dial whatever the coordinator names here, so the check is on
    the shape of the address rather than on trust: file:// and friends are not
    peers, and a URL with no host is a typo that would otherwise come back as
    an obscure transport error.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise UnusableTarget(
            f"Can only dial an http or https address; got {url.strip()!r}."
        )
    if not parsed.hostname:
        raise UnusableTarget(f"No host in {url.strip()!r}.")
    return url.strip().rstrip("/")


async def dial(
    get_json,
    source: str,
    target: str,
    url: str,
    timeout: float = REACH_TIMEOUT_S,
    clock=time.monotonic,
    pair: bool = True,
) -> ReachLeg:
    """GET {url}/agent/health and time it. Never raises.

    `get_json` is the same `AgentClient.get_json` the heartbeat uses, so a leg
    here fails for exactly the reasons a heartbeat fails -- this cannot report
    a node reachable that the health loop is about to mark unhealthy.
    """
    try:
        base = validate_target(url)
    except UnusableTarget as exc:
        return ReachLeg(
            source=source, target=target, url=url, ok=False, error=str(exc), pair=pair
        )

    started = clock()
    try:
        payload = await get_json(f"{base}/agent/health", timeout=timeout)
    except Exception as exc:
        # Round the elapsed time away entirely rather than reporting how long
        # the failure took: it is a timeout, and a number beside "unreachable"
        # invites reading it as latency.
        return ReachLeg(
            source=source, target=target, url=base, ok=False, error=str(exc), pair=pair
        )
    elapsed_ms = round((clock() - started) * 1000, 1)
    answered_as = None
    if isinstance(payload, dict):
        value = payload.get("node_id")
        answered_as = str(value) if value else None
    return ReachLeg(
        source=source,
        target=target,
        url=base,
        ok=True,
        ms=elapsed_ms,
        answered_as=answered_as,
        pair=pair,
    )


def _directions(legs: list[ReachLeg]) -> str:
    return ", ".join(f"{leg.source} → {leg.target}" for leg in legs)


def summarize(legs: list[ReachLeg]) -> tuple[bool, str]:
    """The one-sentence verdict, and whether every dialled leg answered.

    Three outcomes, not two. A direction that FAILED and a direction that could
    not be CHECKED are different findings, and collapsing them loses the one
    piece of information an operator acts on. So:

      - any failure  -> not ok, and the sentence names the failing direction.
        "Not connected" without a direction sends someone to check both
        machines when one of them is demonstrably fine.
      - no failure, some unchecked -> ok, because nothing that was tested came
        back bad, but the sentence never claims "both ways" for a direction
        nobody dialled.
      - everything answered -> ok, and said plainly.

    Only legs BETWEEN the two nodes count toward "reachable". Reaching each of
    them from a third machine is a prerequisite, not an answer.
    """
    dialled = [leg for leg in legs if leg.note is None]
    failed = [leg for leg in dialled if not leg.ok]
    between = [leg for leg in dialled if leg.pair]
    unchecked = [leg for leg in legs if leg.note is not None and not leg.ok]

    tail = f" {_directions(unchecked)} could not be checked." if unchecked else ""

    if failed:
        if len(failed) == len(dialled) and len(dialled) > 1:
            return False, f"Neither direction answered.{tail}"
        if len(failed) == len(dialled):
            return False, f"{_directions(failed)} did not answer.{tail}"
        return False, (
            f"One-way: {_directions(failed)} did not answer, the other direction "
            f"did. The machine that cannot be dialled is the one to look at.{tail}"
        )
    if not between:
        if unchecked:
            reached = (
                " The coordinator reached both machines, which does not "
                "establish that they can reach each other."
                if dialled
                else ""
            )
            return True, (
                f"No direction between them could be tested.{tail}{reached}"
            )
        return True, "Both endpoints are this machine; there is nothing to dial."
    if len(between) == 1:
        leg = between[0]
        return True, f"{leg.source} → {leg.target} answered.{tail}"
    return True, f"Reachable both ways. All {len(between)} probes answered.{tail}"
