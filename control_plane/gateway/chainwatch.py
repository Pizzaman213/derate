"""The audit chain, verified at boot and every ``SINK_HEARTBEAT_S`` after.

``GET /api/audit/integrity`` and ``python3 -m control_plane.auth.verify``
only answer when somebody asks. This asks on its own: a chain that stops
verifying -- a row edited, or the newest rows deleted under the mirror's
``HEAD`` anchor -- is a CRITICAL line on ``gateway.security`` and one
``audit_chain_broken`` audit row, once per distinct ``first_bad_id`` so a
standing break is not re-reported every minute.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict

from control_plane.auditlog import SINK_HEARTBEAT_S
from control_plane.contracts.auth import AuditAction, AuthUnavailable, ChainReport

log = logging.getLogger("gateway.security")

#: The actor on rows the gateway writes about itself.
SYSTEM_ACTOR = "system"


def warn_if_unanchored(env=None) -> bool:
    """WARNING when no remote sink is configured. True when one is."""
    env = os.environ if env is None else env
    if (env.get("DERATE_AUDIT_SINK", "") or "").strip():
        return True
    log.warning("DERATE_AUDIT_SINK is unset: the audit chain head is anchored only on this box")
    return False


class ChainWatch:
    """Verifies the chain of *stores* (an ``AuthStores``) and reports breaks."""

    def __init__(self, stores, *, interval_s: float = SINK_HEARTBEAT_S) -> None:
        self._stores = stores
        self.interval_s = interval_s
        self._reported: set[int | None] = set()

    def check(self) -> ChainReport | None:
        """Verify once. Blocking; None when the auth store cannot be opened."""
        try:
            auth = self._stores.auth
            report = auth.verify_chain()
        except AuthUnavailable as exc:
            log.warning("audit chain not verified: %s", exc.message)
            return None
        if report.ok or report.first_bad_id in self._reported:
            return report
        self._reported.add(report.first_bad_id)
        log.critical(
            "audit chain BROKEN at row %s (%d rows verified before it, anchor %s)",
            report.first_bad_id, report.rows, report.anchor,
        )
        try:
            auth.audit(
                SYSTEM_ACTOR,
                AuditAction.AUDIT_CHAIN_BROKEN,
                "audit",
                str(report.first_bad_id) if report.first_bad_id is not None else "-",
                after=asdict(report),
            )
        except AuthUnavailable as exc:
            log.error("could not record audit_chain_broken: %s", exc.message)
        return report

    async def run(self) -> None:
        """Check every ``interval_s`` until cancelled (the boot check is the
        caller's, awaited before the server binds)."""
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await asyncio.to_thread(self.check)
            except Exception:  # never let the watch die on one bad pass
                log.exception("audit chain verification failed")
