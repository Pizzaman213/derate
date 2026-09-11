"""Disk cache for resolved shapes.

Resolution sits in a UI request path, so the second lookup of a model must not
touch the network. Entries are keyed by model id, revision and any dtype
override. An entry pinned to an exact commit sha (40 hex characters, the only
revision spelling git guarantees is immutable) never expires -- that content
cannot change. An entry for a floating ref -- ``main``, a branch name, a tag --
expires on a TTL. A non-positive TTL does not mean "cache forever": it means
the cache is disabled for floating refs, so every read counts as stale. 0
meaning infinite is the kind of trap a caller reaches for by mistake when they
mean "no TTL configured, so do not cache."
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from control_plane.contracts import ModelShape
from control_plane.paths import data_dir

from .speculators import SpeculativeOption
from .types import (
    ParamSource,
    QuantRequirement,
    QuantSource,
    Resolution,
    RuntimeSupport,
    SupportLevel,
    SupportVerdict,
)

#: Bump when the serialized form or the arithmetic behind it changes, so stale
#: entries are ignored rather than deserialized into a shape that means
#: something different now.
#:
#: 4 is such a change without a field moving. A cached resolution stores facts
#: about the model AND this build's opinions about it -- the support verdict
#: per runtime, and the warning lines derived from it -- so a build that adds a
#: runtime reads yesterday's answer and reports that nothing can load a model
#: it now serves. The entries would have expired on their own within the TTL;
#: this makes it immediate, which matters because the stale reading is
#: `launchable: false` on exactly the models the new runtime exists for.
#:
#: 5 adds `speculators`, and it is the same failure in a new place: an entry
#: written before that field existed deserializes to an empty tuple, which is
#: indistinguishable on the wire from "this checkpoint declares no speculative
#: method" -- a claim that is never true, since ngram needs no model support at
#: all. Expiring them immediately is the only reading that cannot be wrong.
#:
#: 6 adds `target_model_type`, and it is the same failure a third time. An
#: entry written before that field existed deserializes to `""`, which means
#: "this head declared nothing about its target" -- and that is precisely the
#: value that makes `speculators.head_option` let it through. So a vision head
#: cached under schema 5 would keep being offered for a text model of the same
#: width, which is the bug the field was added to fix.
#:
#: 7 adds `routed_expert_params`, and it is the same failure a fourth time.
#: The field decodes to 0 on an entry written before it existed, and 0 means
#: "not derived" -- so the fit gate charges the routed experts whole to every
#: rank, which is exactly the over-count the field was added to end. Unlike
#: the three above this one errs toward refusing rather than inviting, but a
#: cached MoE checkpoint would still be told it needs nodes it does not.
SCHEMA_VERSION = 7

DEFAULT_TTL_SECONDS = float(os.environ.get("DERATE_RESOLVER_TTL", 24 * 3600))

#: A revision only counts as pinned when it is exactly a full commit sha.
#: Anything shorter or differently shaped -- a long branch name included --
#: is a floating ref and must still expire on the TTL.
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def _is_pinned_commit(revision: str) -> bool:
    return bool(revision) and _COMMIT_SHA.fullmatch(revision) is not None


def default_cache_dir() -> Path:
    """Under the data root, unless ``DERATE_CACHE_DIR`` names somewhere else.

    This used to answer the question itself: check ``/data``, fall back to
    ``~/.cache/derate``. ``control_plane/paths.py`` is that logic generalised --
    its docstring says so, naming this function as where it came from -- but
    this one was never moved onto it, so the two disagreed. With no override
    and no writable ``/data``, the estate went to the platform's
    application-state directory and the resolver's cache stayed in
    ``~/.cache``: an operator who set ``DERATE_DATA_DIR`` to move everything
    found this left behind, reachable only through a differently-named
    variable.

    ``DERATE_CACHE_DIR`` still wins, and still means what it always did -- a
    cache is the one part of the estate somebody has a real reason to put on a
    different disk from the rest.
    """
    override = os.environ.get("DERATE_CACHE_DIR")
    if override:
        return Path(override).expanduser() / "resolver"
    return data_dir() / "cache" / "resolver"


def cache_key(model_id: str, revision: str, dtype: str | None) -> str:
    raw = f"v{SCHEMA_VERSION}|{model_id}|{revision}|{dtype or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def to_dict(res: Resolution) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "shape": asdict(res.shape),
        "revision": res.revision,
        "param_source": res.param_source.value,
        "quant_source": res.quant_source.value,
        "support": {
            "architectures": list(res.support.architectures),
            "runtimes": [
                {
                    "runtime": r.runtime,
                    "level": r.level.value,
                    "reason": r.reason,
                    "version": r.version,
                }
                for r in res.support.runtimes
            ],
            "quant": asdict(res.support.quant),
        },
        "warnings": list(res.warnings),
        "weight_bytes": res.weight_bytes,
        "architectures": list(res.architectures),
        "model_type": res.model_type,
        "target_model_type": res.target_model_type,
        "max_position_embeddings": res.max_position_embeddings,
        "param_breakdown": dict(res.param_breakdown),
        "speculators": [opt.as_dict() for opt in res.speculators],
        "resolved_at": res.resolved_at,
    }


def from_dict(data: dict[str, Any]) -> Resolution:
    support = data.get("support", {})
    return Resolution(
        shape=ModelShape(**data["shape"]),
        revision=data.get("revision", "main"),
        param_source=ParamSource(data["param_source"]),
        quant_source=QuantSource(data["quant_source"]),
        support=SupportVerdict(
            architectures=tuple(support.get("architectures", ())),
            runtimes=tuple(
                RuntimeSupport(
                    runtime=r["runtime"],
                    level=SupportLevel(r["level"]),
                    reason=r["reason"],
                    version=r.get("version"),
                )
                for r in support.get("runtimes", [])
            ),
            quant=QuantRequirement(**support["quant"]),
        ),
        warnings=list(data.get("warnings", [])),
        weight_bytes=data.get("weight_bytes"),
        architectures=tuple(data.get("architectures", ())),
        model_type=data.get("model_type", ""),
        target_model_type=data.get("target_model_type", ""),
        max_position_embeddings=data.get("max_position_embeddings"),
        param_breakdown=dict(data.get("param_breakdown", {})),
        # Absent on an entry written before speculative decoding existed. Left
        # empty rather than re-derived here: this function has the stored dict
        # and not the config it came from, and a guess made from the former
        # would be indistinguishable on the wire from a real detection.
        speculators=tuple(
            SpeculativeOption.from_dict(opt) for opt in data.get("speculators", [])
        ),
        resolved_at=data.get("resolved_at", 0.0),
    )


class ShapeCache:
    """Two tiers: an in-process LRU in front of a directory of JSON files."""

    def __init__(
        self,
        directory: Path | str | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        memory_entries: int = 128,
    ) -> None:
        self.directory = Path(directory) if directory else default_cache_dir()
        self.ttl_seconds = ttl_seconds
        self._memory: OrderedDict[str, Resolution] = OrderedDict()
        self._memory_entries = memory_entries

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def _expired(self, res: Resolution, revision: str) -> bool:
        if _is_pinned_commit(revision):
            return False  # pinned to a commit; content cannot change
        if self.ttl_seconds <= 0:
            return True  # no positive TTL configured: cache disabled, always stale
        return (time.time() - res.resolved_at) > self.ttl_seconds

    def get(self, model_id: str, revision: str, dtype: str | None) -> Resolution | None:
        key = cache_key(model_id, revision, dtype)
        cached = self._memory.get(key)
        if cached is not None:
            if self._expired(cached, revision):
                self._memory.pop(key, None)
            else:
                self._memory.move_to_end(key)
                return _as_hit(cached)

        path = self._path(key)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if data.get("schema") != SCHEMA_VERSION:
            return None
        try:
            res = from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None  # a stale shape from an older layout; resolve again
        if self._expired(res, revision):
            return None
        self._remember(key, res)
        return _as_hit(res)

    def put(self, model_id: str, revision: str, dtype: str | None, res: Resolution) -> None:
        key = cache_key(model_id, revision, dtype)
        self._remember(key, res)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(to_dict(res), indent=1)
            # Write and rename, so a reader never sees a half-written entry.
            with tempfile.NamedTemporaryFile(
                "w", dir=self.directory, prefix=f".{key}.", suffix=".tmp", delete=False
            ) as handle:
                handle.write(payload)
                tmp = Path(handle.name)
            os.replace(tmp, self._path(key))
        except OSError:
            pass  # a cache that cannot be written is not a resolution failure

    def _remember(self, key: str, res: Resolution) -> None:
        self._memory[key] = res
        self._memory.move_to_end(key)
        while len(self._memory) > self._memory_entries:
            self._memory.popitem(last=False)

    def clear(self) -> None:
        self._memory.clear()
        try:
            for path in self.directory.glob("*.json"):
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _as_hit(res: Resolution) -> Resolution:
    """A copy marked as served from cache, so callers can tell."""
    return Resolution(
        shape=res.shape,
        revision=res.revision,
        param_source=res.param_source,
        quant_source=res.quant_source,
        support=res.support,
        warnings=list(res.warnings),
        weight_bytes=res.weight_bytes,
        architectures=res.architectures,
        model_type=res.model_type,
        target_model_type=res.target_model_type,
        max_position_embeddings=res.max_position_embeddings,
        param_breakdown=dict(res.param_breakdown),
        speculators=tuple(res.speculators),
        resolved_at=res.resolved_at,
        from_cache=True,
    )
