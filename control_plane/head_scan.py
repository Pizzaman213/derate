"""Find every published draft head for a model, and rank them, without launching.

Launching is the expensive step -- two to fifteen minutes each -- so the useful
question is not "measure everything" but "which few are worth a launch". This
answers that from what is already computable: the hub knows what exists,
``resolver/speculators.py::head_option`` knows whether a head is compatible and
what it weighs, and ``fit/calculator.py`` knows what it could reach if every
draft were accepted.

Top level rather than under ``resolver/`` because it composes two components --
the resolver's shapes and the fit gate's arithmetic -- the same reason
``measurements.py`` and ``metrics_scrape.py`` live here.

**What no amount of arithmetic can say is which head is actually FASTEST.** The
ranking is a ceiling, it rewards a small head with a high k, and within a
method family it barely separates anything: nine EAGLE3 checkpoints of one
training run tie at 426 tok/s for Qwen3-4B. Callers must say so beside it, and
:func:`shortlist` exists because of it.

One more limit worth stating where somebody will read it: the compatibility
gate is ``hidden_size`` and ``vocab_size``, so a head trained for a VISION
model passes for the text model of the same width --
``AngelSlim/Qwen3-VL-4B-Instruct_eagle3`` is offered for ``Qwen3-4B``. Nothing
in the two shapes distinguishes them.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re

# --------------------------------------------------------------------------
# the scan: find every published head for a model, without launching one
# --------------------------------------------------------------------------
#
# Launching is the expensive step -- two to fifteen minutes each -- so the
# useful question is not "measure everything" but "which few are worth a
# launch". Everything needed to answer that is already computable: the hub
# knows what exists, `speculators.head_option` knows whether a head is
# compatible and what it weighs, and `speculative_decode_tps_range` knows what
# it could reach if every draft were accepted.
#
# What no amount of arithmetic can say is which head is actually FASTEST, and
# the scan says so rather than implying otherwise. See `_print_scan`.

#: Name fragments that cannot be a head this runtime loads, used only to skip
#: a resolve. The precedent is `quant_detect.from_name` -- a guess from the
#: name, offered as a guess -- and the direction matters: this may only ever
#: DROP a candidate from consideration, never admit one. Anything that survives
#: is priced properly and can still be refused on evidence.
#:
#: `mlx` is absent because `search_models` already drops it.
NOT_A_HEAD = ("gguf", "-mnn", "onnx", "-ov-", "openvino", "-ov_", "awq-marlin")

#: What to ask the hub. Derived from the model's own short name rather than
#: hard-coded, so this works for a model nobody anticipated.
QUERY_SUFFIXES = ("eagle", "mtp", "speculator", "draft", "dspark", "dflash")

#: Rows of the ranking to print. The shortlist is the answer; the table
#: is context for it, and past a dozen rows it stops being either.
TABLE_ROWS = 12


def short_name(model_id: str) -> str:
    return model_id.split("/")[-1]


def query_stems(model_id: str) -> list[str]:
    """The names to search the hub under. The base name matters more than the id.

    A head is published against the model it was TRAINED on, which is the
    unquantized base -- so searching for `Qwen3-4B-AWQ eagle` finds almost
    nothing while `Qwen3-4B eagle` finds a dozen. That cost this scan 62 of its
    65 candidates the first time it ran.

    The quantization suffix is identified with `quant_detect.from_name` rather
    than a list of my own: it already knows `AWQ`, `FP8`, `GPTQ`, `nvfp4` and
    `4bit`, and it correctly leaves `4B`, `2507`, `Instruct` and `Thinking`
    alone -- which is exactly the line that has to be drawn, and drawing it
    twice is how the two copies come to disagree.

    Both stems are returned, most specific first. A head published for the
    quantized variant specifically (they exist) is still found under the full
    name, and one published for the base is found under the stem.
    """
    from control_plane.resolver.quant_detect import from_name

    short = short_name(model_id)
    segments = short.split("-")
    while len(segments) > 1 and from_name(segments[-1]) is not None:
        segments.pop()
    stem = "-".join(segments)
    return [short] if stem == short else [short, stem]


def candidates(resolver, model_id: str, *, limit: int = 20) -> list[dict]:
    """Every hub repository that might be a draft head for *model_id*.

    Unresolved and unfiltered except by name -- this is the cheap half. One
    search per suffix, deduplicated, with the model itself and anything the
    name rules out removed.
    """
    seen: set[str] = set()
    out: list[dict] = []
    queries = [f"{stem} {suffix}"
               for stem in query_stems(model_id)
               for suffix in QUERY_SUFFIXES]
    for query in queries:
        try:
            rows = resolver.search_models(query, limit=limit)
        except Exception:
            continue  # one query failing is not the scan failing
        for row in rows:
            candidate = row.get("model_id") or ""
            if not candidate or candidate in seen or candidate == model_id:
                continue
            seen.add(candidate)
            # The REPOSITORY name, never the whole id: the organisation
            # `taobao-mnn` publishes ordinary safetensors heads, and matching
            # `-mnn` against the full id dropped two perfectly good EAGLE3
            # heads for the name of the account that published them.
            lowered = short_name(candidate).lower()
            if any(marker in lowered for marker in NOT_A_HEAD):
                out.append({**row, "skipped": "the name says this is not a "
                                              "format the runtime loads as a head"})
                continue
            out.append(row)
    return out


def price(resolver, base_shape, rows: list[dict], specs, *, workers: int = 8,
          target_model_type: str = ""):
    """Resolve and price each candidate. Returns (usable, rejected).

    `head_option` does the deciding: it applies every gate and returns a
    sentence for each failure, so nothing here re-implements compatibility.

    *target_model_type* is the TARGET's own `model_type`, which is what lets a
    head declaring a different one be refused -- the only thing that separates
    a vision head from a text head of the same width. Empty degrades to the
    geometry gates, refusing nothing extra.
    """
    from control_plane.resolver import speculators as spec_detect

    def one(row: dict):
        if row.get("skipped"):
            return row, None, row["skipped"]
        try:
            head = resolver.resolve_full(row["model_id"])
        except Exception as exc:
            return row, None, type(exc).__name__
        try:
            return row, spec_detect.head_option(
                head, base_shape, image_speculators=specs,
                target_model_type=target_model_type,
            ), None
        except Exception as exc:
            return row, None, str(exc)[:100]

    usable, rejected = [], []
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for row, option, why in pool.map(one, rows):
            if option is not None and option.launchable:
                usable.append((row, option))
            else:
                rejected.append((row, why or (option.note if option else "unknown")))
    return usable, rejected


def rank(usable: list, base_shape, bandwidth: float) -> list:
    """(ceiling tok/s, row, option) per head, best first.

    The ceiling is `speculative_decode_tps_range`'s -- every drafted token
    accepted, with the draft's own bandwidth cost already subtracted. It is a
    bound, not a prediction, and `_print_scan` says so beside it.
    """
    from control_plane.fit.calculator import (
        draft_ratio,
        predict_decode_tps,
        speculative_overhead,
    )

    base = predict_decode_tps(base_shape, bandwidth, 0.0)
    scored = []
    for row, option in usable:
        ratio = draft_ratio(base_shape, option.draft_params or 0)
        k = max(1, option.max_tokens)
        ceiling = base * (k + 1) / speculative_overhead(k, ratio)
        scored.append((ceiling, row, option))
    scored.sort(key=lambda r: -r[0])
    return base, scored


def shortlist(scored: list, want: int) -> list:
    """One head per METHOD FAMILY, best-established first.

    Within a family the ceiling barely discriminates -- three EAGLE3 heads for
    Qwen3-4B scored 416, 416 and 405, a spread that is a rounding difference in
    their size and not a reason to prefer one. Measuring all three spends three
    launches to answer one question. Taking the most-downloaded head from each
    family instead spends the same launches on mechanisms that genuinely differ
    in acceptance, which is the thing a launch is the only way to learn.
    """
    by_family: dict[str, tuple] = {}
    for ceiling, row, option in scored:
        family = option.method.value
        current = by_family.get(family)
        if current is None or (row.get("downloads") or 0) > (current[1].get("downloads") or 0):
            by_family[family] = (ceiling, row, option)
    ordered = sorted(by_family.values(), key=lambda r: -(r[1].get("downloads") or 0))
    return ordered[:max(1, want)]


# --------------------------------------------------------------------------
# which one to offer: the ranking cannot answer this, and here is why
# --------------------------------------------------------------------------

#: Why the highest ceiling is not the recommendation.
#:
#: `draft_ratio` is the draft's parameters over the target's active ones, so a
#: draft that reads NO weights has ratio 0, `speculative_overhead(k, 0)` is
#: exactly 1.0, and its ceiling is `base * (k + 1)` -- strictly above any head
#: that has weights, at every k, for every model there has ever been. ngram
#: therefore wins the ranking by construction rather than on merit, and an
#: auto-pick that took the top row would answer "ngram" for everything.
#:
#: It stays in the picker, and it stays in the ranking table, because it is
#: free and on the workloads it suits it is genuinely the right answer. It is
#: only barred from being chosen FOR somebody, which is a different thing and
#: worth keeping distinct.
WEIGHTLESS_NOTE = (
    "ngram costs no memory and is offered below, but it is not recommended "
    "automatically: it drafts by matching the output against the prompt, so it "
    "reads no weights, and a draft with no weights has the highest ceiling here "
    "by arithmetic rather than by being faster. It pays off where output repeats "
    "input -- code edits, extraction, structured formats -- and does nothing on "
    "prose."
)


def recommend(scored: list, measured: dict[str, float] | None = None):
    """The one head to offer as the pick, as `(ceiling, row, option)`. None if none.

    Three rules, in order, and each is here because the one before it cannot
    decide:

    **A weightless draft is never chosen for somebody.** See
    :data:`WEIGHTLESS_NOTE` -- it wins the ceiling ranking by construction.

    **A measurement beats every amount of arithmetic.** *measured* maps a
    method name to the best tok/s a sweep actually got for it on this hardware
    and image. It is keyed by METHOD and not by head repository because
    ``measurements.SpecRecord`` is -- a sweep records "eagle3 reached 118 tok/s
    here", which identifies the mechanism and not which of nine EAGLE3
    checkpoints produced it. So a measurement picks the family and the rule
    below still picks within it.

    **Across families the ceiling decides; within one, downloads do.** That is
    :func:`shortlist`'s existing split and it is reused rather than restated:
    the ceiling ties inside a family (nine EAGLE3 heads at 426 tok/s) and
    genuinely differs between them.
    """
    weighted = [r for r in scored if (r[2].draft_params or 0) > 0]
    if not weighted:
        return None
    if measured:
        seen = [r for r in weighted if r[2].method.value in measured]
        if seen:
            weighted = seen
            best = max(measured[r[2].method.value] for r in weighted)
            weighted = [r for r in weighted
                        if measured[r[2].method.value] >= best]
    per_family = shortlist(weighted, len(weighted))
    return max(per_family, key=lambda r: r[0]) if per_family else None


# --------------------------------------------------------------------------
# remembering a scan: the answer is expensive and it barely moves
# --------------------------------------------------------------------------
#
# A scan is a dozen hub searches plus a resolve per candidate -- 39 candidates
# for Qwen3-30B-A3B -- and the answer only changes when somebody publishes a
# new head. Recomputing it per page view was the reason it had to hide behind
# a button; written down, it is computed once and is simply there afterwards,
# which is what lets the control offer a recommendation without being asked.

#: Under `paths.py::data_dir()`, beside `measurements/`. Same reasoning: one
#: resolver for the root, never a re-typed `/data` fallback.
SCAN_SUBDIR = "scans/heads"

#: Same scrubber as `measurements.py`. Model ids carry `/` and image versions
#: carry `+` and `.`; neither may reach a path component.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def records_dir():
    from control_plane.paths import data_dir

    return data_dir() / SCAN_SUBDIR


def _scan_path(directory, model_id: str, runtime_version: str):
    stem = _UNSAFE.sub("-", f"{model_id}__{runtime_version}").strip("-")[:180]
    return directory / f"{stem}.json"


def save_scan(
    model_id: str,
    runtime_version: str,
    usable: list,
    rejected: list,
    scanned_at: float,
    *,
    directory=None,
) -> None:
    """Write what a scan found. Best effort -- an unwritable estate re-scans.

    **Priced facts, never conclusions.** The ceiling is not stored: it is
    `predict_decode_tps` against the SERVING node's bandwidth, so a stored one
    would be a number the next node invalidates, and recomputing it costs
    nothing. What is expensive is knowing that a repository exists, that this
    image can load its class and what its shards weigh -- and that is exactly
    what goes in.
    """
    target = directory or records_dir()
    payload = {
        "model_id": model_id,
        "runtime_version": runtime_version,
        "scanned_at": scanned_at,
        "heads": [
            {"model_id": row["model_id"],
             "downloads": row.get("downloads"),
             "option": option.as_dict()}
            for row, option in usable
        ],
        "rejected": [
            {"model_id": row.get("model_id", ""), "reason": str(why)}
            for row, why in rejected
        ],
    }
    try:
        target.mkdir(parents=True, exist_ok=True)
        path = _scan_path(target, model_id, runtime_version)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp, path)
    except OSError:
        pass


def cached_scan(model_id: str, runtime_version: str, *, directory=None):
    """A previous scan as `(usable, rejected, scanned_at)`, or None.

    **Keyed by the image version as well as the model, and a mismatch MISSES.**
    Whether a head is loadable at all comes from the image's own speculator
    registry, so an answer computed under a different image is an answer to a
    different question -- the same rule `measurements.matching` enforces, and
    for the same reason: a stale figure presented as current is worse than
    re-doing the work.

    A record written before a field existed raises `KeyError` here and reads as
    a miss, which re-scans. That is the correct outcome and it is why the
    fields below are subscripted rather than `.get()`-ed.
    """
    from control_plane.resolver.speculators import SpeculativeOption

    path = _scan_path(directory or records_dir(), model_id, runtime_version)
    try:
        payload = json.loads(path.read_text())
        usable = [
            ({"model_id": h["model_id"], "downloads": h.get("downloads")},
             SpeculativeOption.from_dict(h["option"]))
            for h in payload["heads"]
        ]
        rejected = [
            ({"model_id": r["model_id"]}, r["reason"]) for r in payload["rejected"]
        ]
        return usable, rejected, float(payload["scanned_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
