"""Limit names: parsing, classification, and the counter each one reads.

A limit is named by a string from ``LIMIT_TYPES``. Fourteen are fixed names;
the per-model ones are ``model:<served_name>:<suffix>``, and a served name may
itself contain ``:`` or ``/`` (``org/model:v1``), so the suffix is split off
the *last* colon. ``parse_limit_type`` folds a per-model name onto its
template base (``model:rpm``) and hands the served name back separately, so
everything downstream switches on a closed set of bases.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from control_plane.contracts.auth import LIMIT_TYPES, MODEL_LIMIT_SUFFIXES

MODEL_PREFIX = "model:"
#: The placeholder the templates in ``LIMIT_TYPES`` carry. A name that still
#: holds it is a form that forgot to substitute a model, not a model.
MODEL_PLACEHOLDER = "<id>"

FIXED_LIMIT_TYPES = tuple(t for t in LIMIT_TYPES if not t.startswith(MODEL_PREFIX))


class LimitKind(str, Enum):
    #: Counted across the 30-day cycle, persisted in ``cycle_counters``.
    CYCLE = "cycle"
    #: A sliding 60-second window, in memory.
    WINDOW = "window"
    #: Requests in flight right now, in memory.
    CONCURRENCY = "concurrency"
    #: A ceiling on one request's size; nothing accumulates.
    REQUEST_CAP = "request_cap"


#: base -> kind. Per-model bases are ``model:<suffix>``.
KIND: dict[str, LimitKind] = {
    "tokens_total_cycle": LimitKind.CYCLE,
    "tokens_input_cycle": LimitKind.CYCLE,
    "tokens_output_cycle": LimitKind.CYCLE,
    "requests_cycle": LimitKind.CYCLE,
    "budget_usd_cycle": LimitKind.CYCLE,
    "local_tokens_cycle": LimitKind.CYCLE,
    "local_usd_cycle": LimitKind.CYCLE,
    "cloud_tokens_cycle": LimitKind.CYCLE,
    "cloud_usd_cycle": LimitKind.CYCLE,
    "rpm": LimitKind.WINDOW,
    "tpm": LimitKind.WINDOW,
    "max_concurrent": LimitKind.CONCURRENCY,
    "max_output_tokens_per_request": LimitKind.REQUEST_CAP,
    "max_prompt_tokens_per_request": LimitKind.REQUEST_CAP,
    "model:tokens_cycle": LimitKind.CYCLE,
    "model:usd_cycle": LimitKind.CYCLE,
    "model:rpm": LimitKind.WINDOW,
    "model:tpm": LimitKind.WINDOW,
}

#: Cycle base -> the ``cycle_counters`` metric it reads. ``{model}`` is the
#: served name for the per-model bases.
CYCLE_METRIC: dict[str, str] = {
    "tokens_total_cycle": "tokens_total",
    "tokens_input_cycle": "tokens_input",
    "tokens_output_cycle": "tokens_output",
    "requests_cycle": "requests",
    "budget_usd_cycle": "usd",
    "local_tokens_cycle": "local_tokens",
    "local_usd_cycle": "local_usd",
    "cloud_tokens_cycle": "cloud_tokens",
    "cloud_usd_cycle": "cloud_usd",
    "model:tokens_cycle": "model:{model}:tokens",
    "model:usd_cycle": "model:{model}:usd",
}

#: Window bases that count requests; the others sum tokens.
COUNTING_WINDOWS = frozenset({"rpm", "model:rpm"})

#: Bases denominated in dollars -- the only ones a reservation's estimated
#: cost is needed for.
USD_BASES = frozenset({"budget_usd_cycle", "local_usd_cycle", "cloud_usd_cycle", "model:usd_cycle"})

#: Bases that only apply to a request on a LOCAL / REMOTE target.
LOCAL_ONLY = frozenset({"local_tokens_cycle", "local_usd_cycle"})
CLOUD_ONLY = frozenset({"cloud_tokens_cycle", "cloud_usd_cycle"})

#: The blocking bases a key's ``cloud_fallback_local_model`` answers.
FALLBACK_BASES = frozenset({"cloud_tokens_cycle", "cloud_usd_cycle", "budget_usd_cycle"})


@lru_cache(maxsize=4096)
def parse_limit_type(name: str) -> tuple[str, str | None]:
    """``(base, served_name | None)``, or ``ValueError`` naming ``name``.

    ``"rpm"`` -> ``("rpm", None)``; ``"model:org/m:v1:tpm"`` ->
    ``("model:tpm", "org/m:v1")``.
    """
    if not isinstance(name, str):
        raise ValueError(f"limit type must be a string, got {name!r}")
    if name in FIXED_LIMIT_TYPES:
        return name, None
    if name.startswith(MODEL_PREFIX):
        served, _, suffix = name[len(MODEL_PREFIX) :].rpartition(":")
        if served and served != MODEL_PLACEHOLDER and suffix in MODEL_LIMIT_SUFFIXES:
            return f"{MODEL_PREFIX}{suffix}", served
    raise ValueError(
        f"unknown limit type {name!r}: expected one of {', '.join(FIXED_LIMIT_TYPES)} "
        f"or model:<served_name>:<{'|'.join(MODEL_LIMIT_SUFFIXES)}>"
    )


def model_limit_type(served_name: str, suffix: str) -> str:
    """The inverse of :func:`parse_limit_type` for a per-model limit."""
    name = f"{MODEL_PREFIX}{served_name}:{suffix}"
    parse_limit_type(name)
    return name


def kind_of(name: str) -> LimitKind:
    return KIND[parse_limit_type(name)[0]]


def cycle_metric(base: str, model: str | None) -> str:
    return CYCLE_METRIC[base].format(model=model)
