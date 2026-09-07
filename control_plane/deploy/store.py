"""Deployment record persistence.

The control plane restarting must not orphan a running backend. Every record
past PLANNED is written to /data/deployments/<id>.json, atomically, so a
half-written file can never be read back as a deployment.

Records carry the full ModelShape, ParallelismPlan and FitResult because
reconcile has to hand Agent G a complete Deployment without re-running the
resolver, the planner, or the fit gate against a cluster that may have
changed shape while we were down.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from control_plane.contracts import (
    Deployment,
    DeploymentState,
    FitResult,
    MemoryBreakdown,
    Modality,
    ModelShape,
    ParallelismKind,
    ParallelismPlan,
    Verdict,
)

from .fsm import PERSISTED, TERMINAL

logger = logging.getLogger(__name__)

#: 2 adds Deployment.modality. decode() defaults it to TEXT, so a v1 record
#: on disk still loads and a downgrade only loses the field.
SCHEMA_VERSION = 2

#: Low-severity finding: delete() had no caller, so FAILED/STOPPED records
#: accumulated on disk forever and were reloaded into memory on every
#: restart. A week is long enough to still have the record around while
#: debugging a launch failure after the fact, short enough that a control
#: plane that has been up for months is not carrying years of dead
#: deployments. reconcile() sweeps for this once per restart -- see
#: DeploymentManager.reconcile.
TERMINAL_RETENTION_S = 7 * 24 * 3600.0


class DeploymentStore:
    """One JSON file per deployment, plus the sparkrun handle we need to
    find it again after a restart."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # -- write ------------------------------------------------------------

    def save(self, deployment: Deployment, handle: dict[str, Any] | None = None) -> Path | None:
        """Persist *deployment*. Returns None for states we do not persist."""
        if deployment.state not in PERSISTED:
            return None
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(deployment.deployment_id)
        payload = {
            "schema": SCHEMA_VERSION,
            "deployment": encode(deployment),
            "handle": handle or {},
        }
        _atomic_write(path, json.dumps(payload, indent=2, sort_keys=True))
        return path

    def delete(self, deployment_id: str) -> None:
        self._path(deployment_id).unlink(missing_ok=True)

    def purge_expired(
        self, retention_s: float = TERMINAL_RETENTION_S, *, now: float | None = None
    ) -> int:
        """Delete terminal (FAILED/STOPPED) records untouched for *retention_s*.

        A record's file is rewritten on every state change (``save``), so its
        mtime is exactly when it settled into whatever state it is currently
        in -- a reliable enough clock without adding a field to the frozen
        Deployment contract. Best-effort like load_all: a record we cannot
        read is left alone rather than guessed at. Returns the count removed.
        """
        if not self.root.is_dir():
            return 0
        cutoff = (now if now is not None else time.time()) - retention_s
        removed = 0
        for path in sorted(self.root.glob("*.json")):
            try:
                if path.stat().st_mtime > cutoff:
                    continue
                payload = json.loads(path.read_text())
                state = DeploymentState(payload["deployment"]["state"])
                deployment_id = payload["deployment"]["deployment_id"]
            except Exception:
                logger.warning("could not evaluate %s for GC, leaving it", path, exc_info=True)
                continue
            if state not in TERMINAL:
                continue
            self.delete(deployment_id)
            removed += 1
        return removed

    # -- read -------------------------------------------------------------

    def load_all(self) -> list[tuple[Deployment, dict[str, Any]]]:
        """Every readable record. A corrupt file is logged and skipped, not
        raised: one bad record must not stop the control plane from adopting
        the deployments that are still running."""
        if not self.root.is_dir():
            return []
        out: list[tuple[Deployment, dict[str, Any]]] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
                out.append((decode(payload["deployment"]), dict(payload.get("handle") or {})))
            except Exception:
                logger.warning("unreadable deployment record %s, skipping", path, exc_info=True)
        return out

    def _path(self, deployment_id: str) -> Path:
        safe = "".join(c for c in deployment_id if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError("unusable deployment_id %r" % deployment_id)
        return self.root / ("%s.json" % safe)


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# -- codec ----------------------------------------------------------------
# Hand-written rather than a generic dataclass walker: the contracts are
# frozen, so the shape is known, and an explicit codec fails loudly when a
# contract does change instead of silently dropping a field.


def encode(d: Deployment) -> dict[str, Any]:
    return {
        "deployment_id": d.deployment_id,
        "served_name": d.served_name,
        "shape": asdict(d.shape),
        "plan": {**asdict(d.plan), "kind": d.plan.kind.value},
        "fit": {
            **asdict(d.fit),
            "verdict": d.fit.verdict.value,
            "breakdown": asdict(d.fit.breakdown),
        },
        "runtime": d.runtime,
        "state": d.state.value,
        "backend_url": d.backend_url,
        "context_length": d.context_length,
        "max_concurrent_seqs": d.max_concurrent_seqs,
        "started_at": d.started_at,
        "last_error": d.last_error,
        "modality": d.modality.value,
    }


def decode(raw: dict[str, Any]) -> Deployment:
    plan_raw = dict(raw["plan"])
    plan_raw.pop("world_size", None)
    fit_raw = dict(raw["fit"])
    fit_raw.pop("ok", None)
    breakdown_raw = dict(fit_raw.pop("breakdown"))
    breakdown_raw.pop("total", None)

    return Deployment(
        deployment_id=raw["deployment_id"],
        served_name=raw["served_name"],
        shape=ModelShape(**raw["shape"]),
        plan=ParallelismPlan(
            **{**plan_raw, "kind": ParallelismKind(plan_raw["kind"])}
        ),
        fit=FitResult(
            **{
                **fit_raw,
                "verdict": Verdict(fit_raw["verdict"]),
                "breakdown": MemoryBreakdown(**breakdown_raw),
            }
        ),
        runtime=raw["runtime"],
        state=DeploymentState(raw["state"]),
        backend_url=raw["backend_url"],
        context_length=raw["context_length"],
        max_concurrent_seqs=raw["max_concurrent_seqs"],
        started_at=raw["started_at"],
        last_error=raw["last_error"],
        # Absent in schema v1. A record written before modality existed was
        # necessarily a text deployment, so the default is also the truth.
        modality=Modality(raw.get("modality", Modality.TEXT.value)),
    )
