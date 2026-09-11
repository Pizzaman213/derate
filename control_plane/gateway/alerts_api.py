"""What is wrong right now: `GET /api/alerts`.

A separate router rather than more of ``internal_api.py``, which is already
four thousand lines that several sessions edit at once -- the same reasoning
``capacity_api`` gives for existing at all.

Registration must stay ABOVE the ``StaticFiles`` mount in ``app.py``. A root
mount catches every path not matched by an EARLIER route, so a router
registered after it silently answers ``index.html``.

**Why this is not `/api/history/events` with a filter.** That route is the
audit trail and is unchanged by this feature. It answers "what happened",
which is a different question from "what is wrong now", and three things stop
it answering the second one:

* It is bounded by a time window. ``connor-pi`` has been down for hours, so a
  ``from=-1h`` query returns nothing and the screen would report a healthy
  cluster.
* It truncates at ``limit``. A ``node_lost`` falling off the oldest end while
  its ``node_recovered`` survives folds to "nothing is wrong" -- absence read
  as zero, in a new place.
* It is off entirely when there is no archive, which is every dev box and the
  whole test suite. A surface that says "a node is down" has to work there.

**And why not the 1 Hz metrics frame.** ``gateway/README.md`` settles it: a
field earns a place on the frame when it is a LEVEL somebody watches change,
and an event when it is a sequence of discrete outcomes. Alerts are plainly
the second. The honest counter -- that "how many are open" IS a level -- does
not survive contact: a client holding the set already knows the count, and it
would cost a shape change on the hottest payload in the product to save a
five-second poll.

The two can never disagree, because the book this reads is fed by the same
events the archive stores.
"""

from __future__ import annotations

import time

from fastapi import APIRouter

from .deps import GatewayContext


def create_router(ctx: GatewayContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/alerts")
    async def alerts() -> dict:
        """Every standing condition, worst first.

        Answers an empty set rather than a 503 when no book is wired. A
        gateway built from the day-0 stub still has this route, and "nothing
        is wrong" is the right answer from a coordinator that is not watching
        anything -- a 503 here would put an error on a screen whose whole job
        is to be empty when things are fine.
        """
        book = getattr(ctx, "alerts", None)
        if book is None:
            now = time.time()
            return {"alerts": [], "observing_since": now, "measured_at": now}

        report = book.report()
        # Label decoration belongs here, not in the book: a node is called
        # what the operator called it, everywhere, and the book has no
        # registry to ask.
        label = getattr(ctx.deps.registry, "label", None)
        if label is not None:
            for row in report["alerts"]:
                if row.get("subject_kind") == "node":
                    try:
                        row["subject_label"] = label(row["subject"]) or row["subject"]
                    except Exception:
                        row["subject_label"] = row["subject"]
        return report

    return router
