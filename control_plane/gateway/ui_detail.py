"""Pure computation for the additive fields the UI needs.

Every function here takes data and returns a plain dict. Nothing touches HTTP,
nothing imports `serialize` or `internal_api`, and nothing raises: a port that
cannot answer produces `None`, never a substituted number.

The reason this module exists is mechanical rather than aesthetic. Four separate
features want to add keys to two files -- `serialize.py` and `internal_api.py`
-- that several sessions edit concurrently. Putting the *computation* here
collapses the edit in those files to a call site and a few named keys, which is
small enough to land in one commit inside a short window. It is the same seam
`serialize.routing_payload`'s `sources=` and `circuits=` sidecar dicts already
established; this just gives the sidecars somewhere to be computed.

The rule every function follows: **`None` means "we do not know", and is never
written as `0`.** A provider that does not account has not spent zero dollars.
A target that has never served a request has not completed zero of them in any
useful sense -- though `completed` genuinely is a counter and starts at 0, which
is why the two are distinguished field by field below rather than by a blanket
policy.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("gateway.ui_detail")

#: Emitted for a provider when the port does no accounting. Named individually
#: rather than built by a loop so this file is greppable for each key.
_UNKNOWN_SPEND: dict[str, Any] = {
    "admitting": None,
    "admission_block": None,
    "daily_budget_usd": None,
    "spend_today_usd": None,
    "tokens_today": None,
    "requests_today": None,
    "unpriced_requests_today": None,
    "retry_in_s": None,
    "model_count": None,
}

#: The eight keys copied out of `provider_public_dict`. Copied one at a time by
#: name -- the dict is never merged, never `update()`d, never iterated -- so
#: `serialize.provider_payload` stays an allowlist and a new field appearing
#: upstream cannot reach the wire by accident.
_SPEND_KEYS = (
    "admitting",
    "admission_block",
    "daily_budget_usd",
    "spend_today_usd",
    "tokens_today",
    "requests_today",
    "unpriced_requests_today",
    "retry_in_s",
    "model_count",
)


def provider_spend(port: Any) -> dict[str, dict[str, Any]]:
    """provider_id -> accounting fields, or `{}` when the port does not account.

    Duck-typed on `public_list`, like every other optional port operation in the
    gateway. `include_models=False` because the payload builds `models` from the
    `Provider` record through its own allowlist; fetching them twice doubles the
    response for nothing.

    Note what is NOT done here: zeros are passed through untouched. A
    `requests_today` of 0 from a live provider is a real observation -- it is the
    literal `else 0` for a day with no ledger entry -- and collapsing it to
    `None` would lose the difference between "no requests today" and "nobody is
    counting". Only a port that cannot answer at all yields nulls.
    """
    lister = getattr(port, "public_list", None)
    if not callable(lister):
        return {}
    try:
        rows = lister(include_models=False)
    except TypeError:
        # An older or narrower implementation without the keyword.
        try:
            rows = lister()
        except Exception:
            log.exception("provider public_list failed")
            return {}
    except Exception:
        log.exception("provider public_list failed")
        return {}

    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        pid = row.get("provider_id")
        if not isinstance(pid, str):
            continue
        out[pid] = {key: row.get(key) for key in _SPEND_KEYS}
    return out


def spend_fields(spend: dict[str, Any] | None) -> dict[str, Any]:
    """The nine keys for one provider, all `None` when there is no accounting."""
    if not spend:
        return dict(_UNKNOWN_SPEND)
    return {key: spend.get(key) for key in _SPEND_KEYS}


def admission_blocks(admission: Any, target_ids: list[str]) -> dict[str, list[str]]:
    """target_id -> why it is not admitting, sorted.

    `AdmissionController.blocks()` returns an unordered `set`, so this sorts:
    an unsorted set reaches JSON in whatever order the hash gave it, and two
    identical states then serialise differently between requests, which makes
    diffing responses useless and caching wrong.

    Only targets with at least one block appear. A READY deployment showing
    `admitting: false` with nothing to explain it is the reported symptom this
    exists to remove.
    """
    blocks_of = getattr(admission, "blocks", None)
    if not callable(blocks_of):
        return {}
    out: dict[str, list[str]] = {}
    for tid in target_ids:
        try:
            reasons = blocks_of(tid)
        except Exception:
            log.exception("admission blocks lookup failed for %s", tid)
            continue
        if reasons:
            out[tid] = sorted(reasons)
    return out


def strength_raw(index: Any) -> dict[str, float]:
    """target_id -> the un-normalized strength score.

    `strength` on the wire is normalized against the strongest target
    cluster-wide, which makes it comparable but unitless. `raw` is the number
    behind it -- and its UNIT CHANGES WITH `strength_source`: tokens/sec when
    measured or predicted, GB/s x gpu_count when bandwidth, a dimensionless 1.0
    when default. It is therefore only meaningful beside its source, and the UI
    must never render one without the other.
    """
    raw = getattr(index, "raw_strength", None)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for tid, score in raw.items():
        value = getattr(score, "raw", None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[tid] = float(value)
    return out


def target_counters(stats: Any, target_ids: list[str]) -> dict[str, dict[str, Any]]:
    """target_id -> per-target request accounting.

    The `None`-vs-`0` split here is deliberate and mirrors `TargetStats`:

    * `completed` / `failed` / `total_tokens` are counters. `0` is a real
      answer, and `completed == 0` is the correct "never served" test.
    * `decode_tps` / `mean_duration_s` / `ttft_ms` are EWMAs that stay `None`
      until observed. `decode_tps` in particular is never `0` through
      `complete()`, so a `0` there would be a number nobody measured.

    `decode_tps` is PER-STREAM decode -- one request's own token rate, averaged
    over requests -- as distinct from `tokens_per_sec`, which is a trailing-window
    sum across every concurrent stream. They coincide only at concurrency 1, and
    inter-token latency is only meaningful from the former.
    """
    peek = getattr(stats, "peek", None)
    if not callable(peek):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for tid in target_ids:
        try:
            st = peek(tid)
        except Exception:
            log.exception("stats lookup failed for %s", tid)
            continue
        if st is None:
            # Never selected. Counters are absent rather than zero, so the UI
            # can tell "new replica" from "replica nothing routes to".
            out[tid] = {
                "completed": None,
                "failed": None,
                "total_tokens": None,
                "decode_tps": None,
                "mean_duration_s": None,
            }
            continue
        out[tid] = {
            "completed": getattr(st, "completed", None),
            "failed": getattr(st, "failed", None),
            "total_tokens": getattr(st, "total_tokens", None),
            "decode_tps": getattr(st, "decode_tps", None),
            "mean_duration_s": getattr(st, "mean_duration_s", None),
        }
    return out
