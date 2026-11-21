"""Deployment lifecycle on the coordinator: idle unload and start at boot.

`IdleMonitor` stops a READY deployment whose `lifecycle.idle_unload_s` has
passed with no request in flight and none admitted. `boot_relaunch` relaunches,
once at coordinator start, the newest record of each served name that asked
for `lifecycle.start_at_boot`. Priority is not enforced here: it only orders
the UI's stop suggestions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from control_plane.contracts import DeploymentState
from control_plane.deploy.relaunch import launch_request_from

from .deps import GatewayContext

log = logging.getLogger("gateway.idle")

#: How often the idle clock is read. Idle windows are 5 minutes and up, so
#: half a minute late is noise.
IDLE_POLL_S = 30.0

_TERMINAL = (DeploymentState.FAILED, DeploymentState.STOPPED)


def idle_reason(idle_unload_s: int) -> str:
    return f"idle for {idle_unload_s // 60} minutes"


class IdleMonitor:
    """Same start()/stop() shape as RestartCoordinator."""

    def __init__(
        self,
        ctx: GatewayContext,
        *,
        interval_s: float = IDLE_POLL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ctx = ctx
        self._interval_s = interval_s
        self._clock = clock
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("idle monitor tick failed")

    async def tick(self) -> list[str]:
        """One pass. Returns the deployment ids it stopped."""
        deployments = self._ctx.deps.deployments
        try:
            rows = list(deployments.list())
        except Exception:
            log.exception("idle monitor could not list deployments")
            return []
        now = self._clock()
        stopped: list[str] = []
        for dep in rows:
            lifecycle = getattr(dep, "lifecycle", None)
            limit = getattr(lifecycle, "idle_unload_s", None)
            if not limit or dep.state is not DeploymentState.READY:
                continue
            st = self._ctx.stats.peek(dep.deployment_id)
            if st is not None and st.outstanding > 0:
                continue
            last = max(
                (st.last_begin_at if st is not None else None) or 0.0,
                dep.started_at or 0.0,
            )
            if now - last < limit:
                continue
            reason = idle_reason(limit)
            try:
                await asyncio.to_thread(
                    deployments.stop, dep.deployment_id, reason=reason
                )
            except Exception:
                # Retried on the next tick: the deployment is still READY.
                log.exception("idle unload of %s failed", dep.deployment_id)
                continue
            log.info("stopped %s: %s", dep.deployment_id, reason)
            stopped.append(dep.deployment_id)
        if stopped:
            rebuild = getattr(self._ctx.router, "rebuild", None)
            if callable(rebuild):
                rebuild(force_scores=True)
        return stopped


async def boot_relaunch(ctx: GatewayContext) -> list[str]:
    """Relaunch each served name whose newest record asked to start at boot.

    Only when that record is terminal and nothing non-terminal answers to the
    name. A refusal is logged with the server's own message and never retried.
    Returns the served names it launched.
    """
    launcher = ctx.launcher
    if launcher is None:
        return []
    try:
        rows = list(ctx.deps.deployments.list())
    except Exception:
        log.exception("boot relaunch could not list deployments")
        return []
    live = {d.served_name for d in rows if d.state not in _TERMINAL}
    newest: dict[str, object] = {}
    for dep in rows:
        cur = newest.get(dep.served_name)
        if cur is None or (dep.started_at or 0.0) > (cur.started_at or 0.0):
            newest[dep.served_name] = dep
    launched: list[str] = []
    for name, dep in newest.items():
        lifecycle = getattr(dep, "lifecycle", None)
        if not getattr(lifecycle, "start_at_boot", False):
            continue
        if dep.state not in _TERMINAL or name in live:
            continue
        try:
            status, body = await launcher(launch_request_from(dep), actor="boot")
        except Exception:
            log.exception("start-at-boot relaunch of %s failed", name)
            continue
        if status != 201:
            message = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
            log.warning(
                "start-at-boot relaunch of %s refused (%s): %s", name, status, message or body
            )
            continue
        launched.append(name)
    return launched
