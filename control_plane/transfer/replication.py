"""Background copies of models to other nodes -- proposed, never assumed.

Two sources say a model is wanted elsewhere, in this order: a deployment that
unloads when idle or starts at boot (its relaunch should find its weights on
every node of its plan), then a pinned model (every other node that could hold
it). `plan_replication` turns that into proposals, each with the bytes to send
and every one of derate's own copies it would delete to keep the target above
its free-space floor (`node_floor`), or into one sentence saying why not.

**Nothing runs without an approval, and an approval names what it saw.**
The operator approves a proposal as it was shown -- the coordinator's revision
and size, the target, its floor and the deletions. If any of those changes
before the copy starts (fewer deletions excepted: a resumed copy finds the
approved ones done), the approval no longer matches and the proposal goes back
to waiting. A failed copy drops its approval too, so a retry is asked for again.
A dismissal is kept against the revision and size it was given for, and the
proposal returns when either changes.

**Bytes are charged in full unless the overlap is proven.** A node whose copy
is at another commit, or whose commit nobody can name, may need every blob of
the new checkpoint while the old ones stay, so only a same-revision partial
copy subtracts what is already there. Room is read from the filesystem the
receiver would actually land that folder on (`?folder=` on the capacity
route), and only derate's copies on that same filesystem count as room.

A pinned model larger than a node's memory is never proposed there; a
deployment's plan already fits across its nodes, so its wants are not judged
against one node's memory.

`ReplicationMonitor` re-plans every `REPLICATION_POLL_S`, starts at most one
approved copy at a time and only while nothing is launching, and gives way to
a launch: a launch's own pre-stage wins, and a background copy is cancelled
unless it is the copy that launch needs. The receiver moves every verified
blob into place as it lands, so a cancelled copy resumes where it stopped.

derate deletes only what it placed (`ReplicaStore`), oldest-used first, and
never a model a live deployment on that node is using.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from control_plane.contracts import DeploymentState
from control_plane.registry import modelcache

from .jobs import CancelRefused, ReplicateRefused

log = logging.getLogger(__name__)

#: The free-space floor's cap. Each node's own floor is `node_floor`: the
#: smaller of this and `REPLICA_FREE_SHARE` of its cache filesystem, so a
#: 256 GiB disk keeps 51.2 GiB free rather than being refused every copy.
REPLICA_FREE_FLOOR = 100 << 30
REPLICA_FREE_SHARE = 0.2
REPLICATION_POLL_S = 30.0
_HUB_ID_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_TERMINAL = (DeploymentState.FAILED, DeploymentState.STOPPED)
_AGENT_TIMEOUT_S = 10.0
_DELETE_TIMEOUT_S = 120.0
_GIB = 1 << 30


def pair_key(folder: str, node_id: str) -> str:
    return f"{folder}@{node_id}"


def node_floor(total: int | None, cap: int = REPLICA_FREE_FLOOR) -> int:
    """The free space a copy must leave on a node; the cap when its size is unknown."""
    if not total or total <= 0:
        return cap
    return min(cap, int(total * REPLICA_FREE_SHARE))


def _gib(n: int) -> str:
    return f"{n / _GIB:.1f}"


# -- what derate placed, approved and was told "not now" ----------------------


class ReplicaStore:
    """`data_dir()/replicas.json`: the copies derate placed, the approvals it
    holds, and the proposals the operator dismissed. Written atomically, 0600."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from control_plane.paths import data_dir

            path = data_dir() / "replicas.json"
        self._path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            data = {}
        for k in ("replicas", "approvals", "dismissed"):
            if not isinstance(data.get(k), dict):
                data[k] = {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return self._load()

    def _update(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            data = self._load()
            fn(data)
            self._save(data)

    def add_replica(self, folder: str, node_id: str, nbytes: int, now: float, *,
                    complete: bool = True, revision: str | None = None) -> None:
        """Recorded when a copy STARTS (complete=False), so a copy that is
        paused or dismissed half-way is still known to be derate's and can be
        cleaned up; completed when it finishes. `revision` is the commit the
        coordinator's `refs/main` named when the copy started; None keeps
        whatever was recorded before."""
        def f(d):
            prior = d["replicas"].get(pair_key(folder, node_id), {})
            d["replicas"][pair_key(folder, node_id)] = {
                "folder": folder, "node_id": node_id, "bytes": int(nbytes),
                "placed_at": prior.get("placed_at", now), "used_at": prior.get("used_at"),
                "complete": complete,
                "revision": revision if revision is not None else prior.get("revision"),
            }
        self._update(f)

    def touch(self, keys: Iterable[str], now: float) -> None:
        keys = list(keys)
        if not keys:
            return

        def f(d):
            for k in keys:
                if k in d["replicas"]:
                    d["replicas"][k]["used_at"] = now
        self._update(f)

    def drop_replicas(self, keys: Iterable[str]) -> None:
        keys = list(keys)
        if keys:
            self._update(lambda d: [d["replicas"].pop(k, None) for k in keys])

    def approve(self, key: str, evict: Sequence[str], now: float,
                fingerprint: dict[str, Any] | None = None) -> None:
        """`fingerprint` is what the operator saw (`approval_fingerprint`); an
        approval whose proposal no longer matches it is withdrawn."""
        def f(d):
            d["approvals"][key] = {**(fingerprint or {}), "evict": sorted(evict), "approved_at": now}
            d["dismissed"].pop(key, None)
        self._update(f)

    def drop_approval(self, key: str) -> None:
        self._update(lambda d: d["approvals"].pop(key, None))

    def dismiss(self, key: str, source_bytes: int, revision: str | None = None) -> None:
        def f(d):
            d["dismissed"][key] = {"bytes": int(source_bytes), "revision": revision}
            d["approvals"].pop(key, None)
        self._update(f)


# -- the plan (pure) -----------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """The coordinator's copy of one folder."""

    state: str  # "complete" | "incomplete" | "absent"
    bytes: int = 0
    revision: str | None = None  # the commit `refs/main` names; None = no ref


@dataclass(frozen=True)
class NodeView:
    node_id: str
    healthy: bool
    memory_bytes: int
    supported: bool
    cache: dict[str, int] | None  # folder -> bytes present; None = unreadable
    free: int | None
    reason: str | None = None  # why cache/free could not be read
    total: int | None = None  # size of the roomiest landing filesystem; None = not reported
    # folder -> snapshot ids (commits) the node holds; None = not reported
    revisions: dict[str, list[str]] | None = None
    # folder -> {"free", "total", "landing"} of the filesystem the receiver
    # lands THAT folder on; a folder absent here is judged on `free`/`total`.
    landing: dict[str, dict[str, Any]] | None = None


@dataclass
class Wanted:
    folder: str
    repo_id: str
    node_id: str
    reasons: list[str] = field(default_factory=list)
    # True when a deployment's plan asks for it: that plan already fits across
    # its nodes, so the folder is not judged against one node's memory.
    from_plan: bool = False


@dataclass
class Proposal:
    folder: str
    repo_id: str
    node_id: str
    bytes: int
    source_bytes: int
    reasons: list[str]
    evict: list[dict[str, Any]]
    free_bytes: int | None
    floor_bytes: int
    source_revision: str | None = None


def approval_fingerprint(p: Proposal) -> dict[str, Any]:
    """What an approval was given for, besides its deletions."""
    return {"source_revision": p.source_revision, "source_bytes": p.source_bytes,
            "node_id": p.node_id, "floor_bytes": p.floor_bytes}


def _dismissal_matches(entry: Any, src: Source) -> bool:
    """A dismissal holds while the coordinator's copy is the one it was given
    for. The older int form recorded only the size."""
    if isinstance(entry, dict):
        return entry.get("bytes") == src.bytes and entry.get("revision") == src.revision
    return entry is not None and entry == src.bytes


@dataclass
class Skip:
    folder: str
    repo_id: str
    node_id: str
    sentence: str
    floor_bytes: int | None = None  # set only when the floor is the reason


def wanted_pairs(
    deployments: Sequence[Any], pins: Sequence[str], node_ids: Sequence[str], coordinator_id: str
) -> list[Wanted]:
    """Deployment plans first, then pins; one entry per (folder, node), reasons merged."""
    out: dict[str, Wanted] = {}

    def add(model_id: str, node_id: str, reason: str, from_plan: bool = False) -> None:
        if node_id == coordinator_id or not _HUB_ID_RE.match(model_id or ""):
            return
        folder = modelcache.folder_for(model_id)
        w = out.setdefault(pair_key(folder, node_id), Wanted(folder, model_id, node_id))
        w.from_plan = w.from_plan or from_plan
        if reason not in w.reasons:
            w.reasons.append(reason)

    newest: dict[str, Any] = {}
    for dep in deployments:
        prior = newest.get(dep.served_name)
        if prior is None or (dep.started_at or 0.0) >= (prior.started_at or 0.0):
            newest[dep.served_name] = dep
    for dep in newest.values():
        lc = getattr(dep, "lifecycle", None)
        if not (getattr(lc, "idle_unload_s", None) or getattr(lc, "start_at_boot", False)):
            continue
        why = "starts at boot" if getattr(lc, "start_at_boot", False) else "unloads when idle"
        for node_id in dep.plan.node_ids:
            add(dep.shape.model_id, node_id,
                f"deployment {dep.served_name} {why} and relaunches on {node_id}", from_plan=True)
    for model_id in pins:
        for node_id in node_ids:
            add(model_id, node_id, "pinned")
    return list(out.values())


#: The reason a node that already holds a folder is proposed a copy again.
STALE_REASON = "the coordinator has a newer revision"


def _older_revision(folder: str, src: Source, node: NodeView, record: dict[str, Any] | None) -> bool:
    """True when the node's copy is pinned to another commit than the
    coordinator's `refs/main`: its snapshots lack that commit, or derate's own
    record of the copy names a different one. Unknown on either side is never
    stale -- a node that did not list its snapshots, or lists none (a copy
    still landing), is not proven old."""
    if not src.revision:
        return False
    listed = (node.revisions or {}).get(folder)
    if listed and src.revision not in listed:
        return True
    recorded = (record or {}).get("revision")
    return bool(recorded) and recorded != src.revision


def _same_revision(folder: str, src: Source, node: NodeView, record: dict[str, Any] | None) -> bool:
    """True only when the node's bytes are PROVEN to be of the coordinator's
    commit -- the node lists it, or derate's record of the copy names it.
    Anything less leaves the overlap unknown."""
    if not src.revision:
        return False
    if src.revision in ((node.revisions or {}).get(folder) or []):
        return True
    return (record or {}).get("revision") == src.revision


def plan_replication(
    wanted: Sequence[Wanted],
    sources: dict[str, Source],
    nodes: dict[str, NodeView],
    replicas: dict[str, dict[str, Any]],
    in_use: set[tuple[str, str]],
    dismissed: dict[str, Any],
    floor: int = REPLICA_FREE_FLOOR,
) -> tuple[list[Proposal], list[Skip]]:
    proposals: list[Proposal] = []
    # A copy something still asks for is never deleted to make room for
    # another: that would only propose copying it back on the next pass.
    wanted_here = {(w.folder, w.node_id) for w in wanted}
    skipped: list[Skip] = []
    for w in wanted:
        def skip(sentence: str, floor_bytes: int | None = None) -> None:
            skipped.append(Skip(w.folder, w.repo_id, w.node_id, sentence, floor_bytes))

        src = sources.get(w.folder, Source("absent"))
        if src.state == "absent":
            skip(f"{w.repo_id} is not in the coordinator's cache, so there is nothing to copy.")
            continue
        if src.state == "incomplete":
            skip(f"{w.repo_id} is still downloading on the coordinator; it is proposed once complete.")
            continue
        node = nodes.get(w.node_id)
        if node is None:
            skip(f"{w.node_id} is not a member of this cluster.")
            continue
        if not node.healthy:
            skip(f"{w.node_id} is not answering health checks, so nothing is copied to it.")
            continue
        if not node.supported:
            skip(f"{w.node_id}'s agent has no copy receiver (it runs an older build); "
                 "upgrade it to copy models there.")
            continue
        if node.cache is None or node.free is None:
            skip(f"{w.node_id}'s model cache could not be read: {node.reason or 'no answer'}")
            continue
        present = node.cache.get(w.folder, 0)
        key = pair_key(w.folder, w.node_id)
        record = replicas.get(key)
        reasons = list(w.reasons)
        stale = bool(present) and _older_revision(w.folder, src, node, record)
        if stale:
            reasons.append(STALE_REASON)
        elif present >= src.bytes:
            continue  # already there
        if not w.from_plan and node.memory_bytes and src.bytes > node.memory_bytes:
            skip(f"{w.repo_id}'s weights ({_gib(src.bytes)} GiB) are more than {w.node_id}'s "
                 f"{_gib(node.memory_bytes)} GiB of memory, so it could never be served there.")
            continue
        if _dismissal_matches(dismissed.get(key), src):
            skip(f"You chose not now for {w.repo_id} on {w.node_id}; it is proposed again "
                 "if the coordinator's copy changes.")
            continue
        # Another commit's blobs (or blobs of a commit nobody can name) may
        # share nothing with this one and stay where they are: charge it all.
        if present and not stale and _same_revision(w.folder, src, node, record):
            need = max(0, src.bytes - present)
        else:
            need = src.bytes
        landing = (node.landing or {}).get(w.folder)
        free = int(landing["free"]) if landing else node.free
        here = node_floor(landing.get("total") if landing else node.total, floor)
        where = landing.get("landing") if landing else None
        evict: list[dict[str, Any]] = []
        room = free - need
        if room < here:
            mine = sorted(
                (
                    r for r in replicas.values()
                    if r.get("node_id") == w.node_id
                    and (r.get("folder"), w.node_id) not in wanted_here
                    and (r.get("folder"), w.node_id) not in in_use
                    and r.get("folder") in node.cache
                    # Deleting a copy on another filesystem frees nothing here.
                    and _same_filesystem(where, (node.landing or {}).get(r.get("folder")))
                ),
                key=lambda r: (r.get("used_at") or r.get("placed_at") or 0.0, r.get("folder")),
            )
            for r in mine:
                if room >= here:
                    break
                size = node.cache.get(r["folder"], int(r.get("bytes") or 0))
                evict.append({"folder": r["folder"],
                              "repo_id": modelcache.repo_from_folder(r["folder"]), "bytes": size})
                room += size
            if room < here:
                freed = sum(e["bytes"] for e in evict)
                skip(f"Copying {w.repo_id} ({_gib(need)} GiB) would leave {w.node_id} below its "
                     f"{_gib(here)} GiB free-space floor: it has {_gib(free)} GiB free"
                     f"{f' in {where}' if where else ''}, "
                     f"and derate's own copies there would free only {_gib(freed)} GiB more.",
                     here)
                continue
        proposals.append(Proposal(
            folder=w.folder, repo_id=w.repo_id, node_id=w.node_id, bytes=need,
            source_bytes=src.bytes, reasons=reasons, evict=evict, free_bytes=free,
            floor_bytes=here, source_revision=src.revision,
        ))
    return proposals, skipped


def _same_filesystem(where: str | None, other: dict[str, Any] | None) -> bool:
    """Whether deleting a folder reported at `other` frees room in `where`;
    unknown on either side keeps the folder-less answer (it counts)."""
    if not where or not other or not other.get("landing"):
        return True
    return other["landing"] == where


# -- talking to node agents ------------------------------------------------------


class AgentReader:
    """The agent calls the monitor makes, synchronous, token-bearing."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._revisions: dict[str, dict[str, list[str]]] = {}

    def _target(self, node_id: str) -> tuple[str, dict[str, str]]:
        url = self._registry.agent_url(node_id)
        token = self._registry.agent_token(node_id)
        if not url or not token:
            raise RuntimeError(f"no agent address or credential is held for {node_id}")
        return url.rstrip("/"), {"X-Derate-Token": token}

    def cache(self, node_id: str) -> dict[str, int]:
        import httpx

        url, headers = self._target(node_id)
        res = httpx.get(f"{url}/agent/models/cache", headers=headers, timeout=_AGENT_TIMEOUT_S)
        res.raise_for_status()
        body = res.json()
        if not body.get("available", False):
            if body.get("path") is None:
                return {}  # nothing downloaded there yet
            raise RuntimeError(body.get("reason") or "the cache is unavailable")
        repos = body.get("repos", [])
        self._revisions[node_id] = {
            r["folder"]: [str(x) for x in r.get("revisions") or []] for r in repos
        }
        return {r["folder"]: int(r.get("bytes") or 0) for r in repos}

    def revisions(self, node_id: str) -> dict[str, list[str]] | None:
        """The snapshot ids per folder from the last `cache` answer for this
        node -- the same response, so no second request; None before one."""
        return self._revisions.get(node_id)

    def capacity(self, node_id: str) -> dict[str, int | None] | None:
        """`{"free", "total"}` bytes of the node's roomiest landing folder -- the hub
        cache, or a `DERATE_EXTRA_CACHE_DIRS` folder the receiver falls back to when
        the hub's filesystem is short; None when the agent is too old to say.
        `total` is None from an agent that sends only `free`."""
        import httpx

        url, headers = self._target(node_id)
        res = httpx.get(f"{url}/agent/transfers/capacity", headers=headers, timeout=_AGENT_TIMEOUT_S)
        if res.status_code == 404:
            return None
        res.raise_for_status()
        body = res.json()
        total = body.get("total")
        return {"free": int(body["free"]), "total": int(total) if total is not None else None}

    def landing(self, node_id: str, folder: str, nbytes: int = 0) -> dict[str, Any] | None:
        """`{"free", "total", "landing"}` of the filesystem the receiver would
        land `folder` on -- where it already is, else where a new copy of
        `nbytes` goes (a big one may land on an extra folder, not the hub).
        None from an agent that cannot say (400/404, or no `landing` in the
        answer): the caller keeps the folder-less `capacity`."""
        import httpx

        url, headers = self._target(node_id)
        res = httpx.get(f"{url}/agent/transfers/capacity", params={"folder": folder, "bytes": int(nbytes)},
                        headers=headers, timeout=_AGENT_TIMEOUT_S)
        if res.status_code in (400, 404):
            return None
        res.raise_for_status()
        body = res.json()
        if not body.get("landing"):
            return None
        total = body.get("total")
        return {"free": int(body["free"]), "total": int(total) if total is not None else None,
                "landing": str(body["landing"])}

    def delete(self, node_id: str, folder: str) -> None:
        import httpx

        url, headers = self._target(node_id)
        res = httpx.delete(
            f"{url}/agent/models/cache/{quote(folder, safe='')}", headers=headers,
            timeout=_DELETE_TIMEOUT_S,
        )
        if res.status_code >= 400:
            raise RuntimeError(f"{node_id} refused to delete {folder}: {res.text.strip()[:300]}")


# -- the loop ------------------------------------------------------------------


class ProposalNotFound(LookupError):
    pass


class PlanChanged(ValueError):
    pass


class ReplicationMonitor:
    def __init__(
        self,
        *,
        deployments: Any,
        registry: Any,
        replicator: Any,
        pins: Callable[[], Sequence[str]],
        store: ReplicaStore | None = None,
        agents: Any = None,
        cache_dir: Callable[[], Path | None] = modelcache.cache_dir,
        clock: Callable[[], float] = time.time,
        interval_s: float = REPLICATION_POLL_S,
        floor: int = REPLICA_FREE_FLOOR,
    ) -> None:
        self._deployments = deployments
        self._registry = registry
        self._replicator = replicator
        self._pins = pins
        self._store = store or ReplicaStore()
        self._agents = agents or AgentReader(registry)
        self._cache_dir = cache_dir
        self._clock = clock
        self._interval_s = interval_s
        self._floor = floor
        self._lock = threading.RLock()
        self._proposals: dict[str, Proposal] = {}
        self._skipped: list[Skip] = []
        self._checked_at: float | None = None
        self._running: dict[str, str] = {}  # pair key -> transfer_id
        self._errors: dict[str, str] = {}
        self._unsupported: dict[str, str] = {}  # node -> build it was refused on
        self._task: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------------

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
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("replication tick failed")
            await asyncio.sleep(self._interval_s)

    async def tick(self) -> None:
        await asyncio.to_thread(self.step)

    # -- one pass ------------------------------------------------------------

    def step(self) -> None:
        with self._lock:
            deps = list(self._deployments.list())
            states = {n.profile.node_id: n for n in self._registry.list_nodes()}
            self._follow_running(states)
            launching = [d for d in deps if d.state is DeploymentState.LAUNCHING]
            self._yield_to(launching)

            coordinator = self._replicator.local_node_id
            others = [n for n in states if n != coordinator]
            wanted = wanted_pairs(deps, list(self._pins() or []), others, coordinator)
            in_use = {
                (modelcache.folder_for(d.shape.model_id), n)
                for d in deps if d.state not in _TERMINAL for n in d.plan.node_ids
            }
            stored = self._store.snapshot()
            replicas = stored["replicas"]
            sources = self._sources({w.folder for w in wanted})
            # folder -> bytes a copy would need there (the whole source: the
            # receiver picks a new folder's disk by size); 0 for a placed copy.
            asked: dict[str, dict[str, int]] = {}
            for r in replicas.values():
                asked.setdefault(r["node_id"], {})[r["folder"]] = 0
            for w in wanted:
                asked.setdefault(w.node_id, {})[w.folder] = sources.get(w.folder, Source("absent")).bytes
            views = self._views(states, set(asked), asked)
            self._reconcile_replicas(replicas, views, in_use)
            stored = self._store.snapshot()
            proposals, skipped = plan_replication(
                wanted, sources, views,
                stored["replicas"], in_use, stored["dismissed"], self._floor,
            )
            fresh = {pair_key(p.folder, p.node_id): p for p in proposals}
            # A running copy places each blob as it verifies, so the node can
            # list the whole folder while the job is still committing. Keep its
            # row (and its source size) until the job itself says done.
            for key in self._running:
                if key not in fresh and key in self._proposals:
                    fresh[key] = self._proposals[key]
            self._proposals = fresh
            self._skipped = skipped
            self._checked_at = self._clock()
            self._expire_stale_approvals(stored["approvals"])
            if not launching:
                self._start_next(self._store.snapshot()["approvals"])

    def _follow_running(self, states: dict[str, Any]) -> None:
        for key, tid in list(self._running.items()):
            job = self._replicator.get(tid)
            folder, _, node_id = key.partition("@")
            if job is None:  # cancelled: the approval stands, it runs again later
                del self._running[key]
            elif job["status"] == "done":
                # The folder's size, not the job's: a resumed copy sends only the rest.
                prior = self._proposals.get(key)
                size = prior.source_bytes if prior else int(job.get("total") or 0)
                self._store.add_replica(folder, node_id, size, self._clock(), complete=True,
                                        revision=job.get("revision"))
                self._store.drop_approval(key)
                self._errors.pop(key, None)
                del self._running[key]
                log.info("background copy of %s to %s finished", folder, node_id)
            elif job["status"] == "error":
                self._errors[key] = job.get("error") or "the copy failed"
                self._store.drop_approval(key)
                if "no /agent/transfers route" in self._errors[key]:
                    state = states.get(node_id)
                    self._unsupported[node_id] = getattr(state, "build", "") if state else ""
                del self._running[key]

    def _yield_to(self, launching: Sequence[Any]) -> None:
        if not launching or not self._running:
            return
        needed = {
            pair_key(modelcache.folder_for(d.shape.model_id), n)
            for d in launching for n in d.plan.node_ids
        }
        for key, tid in list(self._running.items()):
            if key in needed:
                continue  # the launch's pre-stage is waiting on this very copy
            try:
                self._replicator.cancel(tid)
            except CancelRefused:
                continue  # verifying: it finishes, and is followed to done
            del self._running[key]
            log.info("paused background copy %s for a launch; it resumes later", key)

    def _views(self, states: dict[str, Any], node_ids: set[str],
               folders: dict[str, dict[str, int]] | None = None) -> dict[str, NodeView]:
        """`folders` names, per node, the folders whose landing filesystem to
        ask for and the bytes a copy of each would need."""
        views: dict[str, NodeView] = {}
        for node_id in node_ids:
            state = states.get(node_id)
            if state is None:
                continue
            build = getattr(state, "build", "")
            if node_id in self._unsupported and self._unsupported[node_id] != build:
                del self._unsupported[node_id]  # a new build: ask again
            memory = max(int(state.profile.total_memory or 0), int(getattr(state, "memory_total", 0) or 0))
            supported = node_id not in self._unsupported
            cache = free = total = reason = revisions = None
            landing: dict[str, dict[str, Any]] = {}
            if state.healthy and supported:
                try:
                    room = self._agents.capacity(node_id)
                    if room is None:
                        supported = False
                        self._unsupported[node_id] = build
                    else:
                        free, total = room["free"], room.get("total")
                        cache = self._agents.cache(node_id)
                        revisions = self._agents.revisions(node_id)
                        for folder, nbytes in sorted((folders or {}).get(node_id, {}).items()):
                            got = self._agents.landing(node_id, folder, nbytes)
                            if got is None:
                                break  # an older agent: every folder gets the node's answer
                            landing[folder] = got
                except Exception as exc:  # noqa: BLE001 - one sentence on screen
                    reason = str(exc) or type(exc).__name__
            views[node_id] = NodeView(node_id, bool(state.healthy), memory, supported, cache, free,
                                      reason, total, revisions, landing or None)
        return views

    def _sources(self, folders: Iterable[str]) -> dict[str, Source]:
        cache = self._cache_dir()
        out: dict[str, Source] = {}
        for folder in folders:
            repo = Path(cache) / folder if cache else None
            if repo is None or not (repo / "snapshots").is_dir():
                out[folder] = Source("absent")
                continue
            try:
                incomplete = any(p.name.endswith(".incomplete") for p in (repo / "blobs").iterdir())
            except OSError:
                incomplete = True
            if incomplete:
                out[folder] = Source("incomplete")
                continue
            out[folder] = Source("complete", modelcache._blob_bytes(repo, follow_symlinks=True)[0],
                                 modelcache.main_commit(repo))
        return out

    def _reconcile_replicas(self, replicas: dict[str, dict], views: dict[str, NodeView],
                            in_use: set[tuple[str, str]]) -> None:
        gone = [
            k for k, r in replicas.items()
            if (v := views.get(r["node_id"])) is not None and v.cache is not None
            and r["folder"] not in v.cache and k not in self._running
        ]
        self._store.drop_replicas(gone)
        self._store.touch(
            [k for k, r in replicas.items() if (r["folder"], r["node_id"]) in in_use and k not in gone],
            self._clock(),
        )

    def _expire_stale_approvals(self, approvals: dict[str, dict]) -> None:
        for key, appr in approvals.items():
            p = self._proposals.get(key)
            if p is None or key in self._running:
                continue
            seen = approval_fingerprint(p)
            if any(k not in appr or appr[k] != v for k, v in seen.items()):
                self._store.drop_approval(key)
                self._errors[key] = (
                    f"the coordinator's copy of {p.repo_id} or {p.node_id}'s room for it changed "
                    "since you approved it; review it again"
                )
                continue
            # Fewer deletions than approved is still covered (a resumed copy
            # finds the approved ones already done); any NEW deletion is not.
            if {e["folder"] for e in p.evict} - set(appr.get("evict", [])):
                self._store.drop_approval(key)
                self._errors[key] = (
                    "the copies it needs to delete changed since you approved it; review it again"
                )

    def _start_next(self, approvals: dict[str, dict]) -> None:
        if self._running or any(j["status"] in ("running", "verifying") for j in self._replicator.list()):
            return
        for key, p in self._proposals.items():
            if key not in approvals:
                continue
            for e in p.evict:
                try:
                    self._agents.delete(p.node_id, e["folder"])
                except Exception as exc:  # noqa: BLE001
                    self._errors[key] = str(exc)
                    self._store.drop_approval(key)
                    return
                self._store.drop_replicas([pair_key(e["folder"], p.node_id)])
                log.info("deleted derate's copy of %s on %s (approved)", e["folder"], p.node_id)
            try:
                job = self._replicator.start(p.folder, p.node_id)
            except ReplicateRefused as exc:
                self._errors[key] = exc.sentence
                self._store.drop_approval(key)
                return
            self._running[key] = job["transfer_id"]
            self._store.add_replica(p.folder, p.node_id, p.source_bytes - p.bytes, self._clock(),
                                    complete=False, revision=job.get("revision"))
            self._errors.pop(key, None)
            log.info("started background copy of %s to %s", p.folder, p.node_id)
            return

    # -- the HTTP half -------------------------------------------------------

    def approve(self, folder: str, node_id: str, evict: Sequence[str]) -> dict[str, Any]:
        key = pair_key(folder, node_id)
        with self._lock:
            p = self._proposals.get(key)
            if p is None:
                raise ProposalNotFound(f"No proposal to copy {folder} to {node_id}.")
            if sorted(evict) != sorted(e["folder"] for e in p.evict):
                raise PlanChanged(
                    "The copies this needs to delete have changed since this page loaded; "
                    "review the new list before approving."
                )
            self._store.approve(key, evict, self._clock(), approval_fingerprint(p))
            self._errors.pop(key, None)
            return self._row(key, p, self._store.snapshot()["approvals"])

    def dismiss(self, folder: str, node_id: str) -> bool:
        key = pair_key(folder, node_id)
        with self._lock:
            p = self._proposals.get(key)
            if p is None:
                return False
            tid = self._running.pop(key, None)
            if tid is not None:
                try:
                    self._replicator.cancel(tid)
                except CancelRefused:
                    self._running[key] = tid  # verifying: it finishes and is recorded
            self._store.dismiss(key, p.source_bytes, p.source_revision)
            del self._proposals[key]
            return True

    def _row(self, key: str, p: Proposal, approvals: dict[str, dict]) -> dict[str, Any]:
        status = "copying" if key in self._running else "approved" if key in approvals else "awaiting_approval"
        row = asdict(p)
        row.pop("source_bytes")
        row.pop("source_revision")
        row.update(status=status, transfer_id=self._running.get(key), last_error=self._errors.get(key))
        return row

    def state(self) -> dict[str, Any]:
        with self._lock:
            stored = self._store.snapshot()
            return {
                "enabled": True,
                "floor_bytes": self._floor,
                "checked_at": self._checked_at,
                "proposals": [self._row(k, p, stored["approvals"]) for k, p in self._proposals.items()],
                "skipped": [asdict(s) for s in self._skipped],
                "replicas": [
                    {**r, "repo_id": modelcache.repo_from_folder(r["folder"])}
                    for r in sorted(stored["replicas"].values(), key=lambda r: (r["node_id"], r["folder"]))
                ],
            }

    def awaiting(self) -> int:
        with self._lock:
            approvals = self._store.snapshot()["approvals"]
            return sum(1 for k in self._proposals if k not in approvals and k not in self._running)
