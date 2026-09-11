"""Measured speculative-decoding records: written by the sweep, read by the card.

The fit gate states a floor and a ceiling for speculative decoding and says, in
its own sentence, that the acceptance rate deciding between them is not
measured. This is where a measurement goes when somebody runs one.

**A record is a fact about one workload on one machine with one image, and the
key says so.** Acceptance on code that mostly copies its input is not
acceptance on prose, a GB10's bandwidth is not a discrete card's, and a vLLM
release can change how a head drafts. Any of those differing makes the stored
number a different measurement, so it is keyed by all of them and a mismatch
MISSES rather than approximating -- the range is still true when there is no
record, and a stale figure presented as current is worse than no figure.

**It never replaces the range.** ``speculative_decode_tps_range`` keeps stating
what is true for a workload nobody measured; a record adds a line beside it
naming the workload that was. That is the whole reason the two live apart.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from control_plane.paths import data_dir

log = logging.getLogger(__name__)

#: One directory, under the same root everything else in the estate uses.
#: `paths.py::data_dir` is the single resolver -- eleven modules used to
#: re-type the `/data` fallback and every one of them failed differently off
#: Linux.
SUBDIR = "measurements/spec"

#: Anything outside this is replaced in a filename. Model ids carry `/` and
#: image versions carry `+` and `.`; neither may reach a path component.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def records_dir() -> Path:
    return data_dir() / SUBDIR


@dataclass(frozen=True)
class SpecRecord:
    """One sweep result: what was run, on what, and what came out."""

    model_id: str
    method: str
    workload: str
    #: The hardware the number is about. A measurement does not travel between
    #: unlike machines: decode is bandwidth-bound, so the same acceptance on a
    #: different part is a different tok/s.
    gpu_name: str
    memory_bandwidth_gbps: float
    #: `ImageProbe.version`. A release that changes how a head drafts changes
    #: the acceptance it gets.
    runtime_version: str
    #: What was launched, and what the curve says is best. These differ often
    #: -- launching at 10 and finding the peak at 3 is the normal outcome, and
    #: it is why one launch answers for every k.
    launched_k: int
    best_k: int
    best_tps: float
    baseline_tps: float
    #: Fraction of draft rounds in which each position was accepted, 0-indexed
    #: and cumulative -- vLLM's `num_accepted_tokens_per_pos / num_drafts`.
    accept_cumulative: list[float]
    mean_acceptance: float | None
    #: Draft rounds behind the figures. A short run is a noisy one and the
    #: reader is entitled to know before trusting a third decimal place.
    drafts: float
    measured_at: float
    #: Which tier of engine evidence this came from -- `per_pos` or
    #: `aggregate`. See `metrics_scrape.SpecDecode.basis`.
    basis: str = "per_pos"
    notes: list[str] = field(default_factory=list)

    @property
    def speedup(self) -> float | None:
        if self.baseline_tps <= 0:
            return None
        return self.best_tps / self.baseline_tps

    def key(self) -> str:
        return "|".join(
            (
                self.model_id,
                self.method,
                self.workload,
                self.gpu_name,
                f"{self.memory_bandwidth_gbps:.0f}",
                self.runtime_version,
            )
        )

    def filename(self) -> str:
        parts = (self.model_id, self.method, self.workload, self.gpu_name,
                 self.runtime_version)
        return _UNSAFE.sub("-", "__".join(parts)).strip("-")[:180] + ".json"


def save(record: SpecRecord, *, directory: Path | None = None) -> Path | None:
    """Write one record. Best effort: an unwritable estate is not a failed run.

    The sweep has already printed everything this holds by the time it is
    called, so losing the file costs the citation on the card and nothing that
    was measured.
    """
    target = (directory or records_dir())
    try:
        target.mkdir(parents=True, exist_ok=True)
        path = target / record.filename()
        path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True))
        return path
    except OSError:
        log.warning("could not write the speculative measurement", exc_info=True)
        return None


def load_all(*, directory: Path | None = None) -> list[SpecRecord]:
    """Every readable record. A corrupt file is skipped, never raised.

    Same rule as `deploy/store.py::load_all`: one bad file must not take out
    the answer for every other measurement on the box.
    """
    target = directory or records_dir()
    if not target.is_dir():
        return []
    out: list[SpecRecord] = []
    for path in sorted(target.glob("*.json")):
        try:
            out.append(SpecRecord(**json.loads(path.read_text())))
        except (OSError, ValueError, TypeError):
            log.warning("skipping unreadable measurement %s", path, exc_info=True)
    return out


def matching(
    model_id: str,
    method: str,
    *,
    gpu_name: str | None,
    memory_bandwidth_gbps: float | None,
    runtime_version: str | None,
    directory: Path | None = None,
) -> list[SpecRecord]:
    """Records for this model and method ON THIS HARDWARE, newest first.

    Every criterion that is `None` is a criterion nobody could check, and it
    stops narrowing rather than excluding everything: a coordinator that could
    not probe its image still shows what was measured, and the record carries
    the version it was measured under so a reader can see the difference.

    Returns every workload that matched. Which to show is the caller's
    decision, and showing one of three would be picking a number.
    """
    rows = [r for r in load_all(directory=directory)
            if r.model_id == model_id and r.method == method]
    if gpu_name:
        rows = [r for r in rows if r.gpu_name == gpu_name]
    if memory_bandwidth_gbps:
        rows = [r for r in rows
                if abs(r.memory_bandwidth_gbps - memory_bandwidth_gbps) < 1.0]
    if runtime_version:
        rows = [r for r in rows if r.runtime_version == runtime_version]
    return sorted(rows, key=lambda r: r.measured_at, reverse=True)


# ---------------------------------------------------------------------------
# Measured decode rate
#
# `fit.predict_decode_tps` is arithmetic: active weights plus the cache for one
# sequence, over the node's bandwidth, times a single global DECODE_EFFICIENCY
# constant. It has never been checked against anything, and on the first
# deployment anyone checked -- Qwen3-0.6B at 8192 context on a GB10 -- it said
# 61.5 tok/s against a measured 120.5.
#
# Two of that gap are modelling, one is irreducible. The prediction charges the
# cache for a FULL 8192-token sequence, so it answers "the rate once the
# context is full" rather than the rate a 300-token request sees; and
# DECODE_EFFICIENCY is one number for every model, kernel and quantization,
# where this deployment implies 0.68. What is left after both is kernel
# behaviour that no closed form predicts.
#
# So the accurate number is not a better constant. It is a measurement, and
# these records are where one goes -- the same argument, and deliberately the
# same shape, as SpecRecord above.

DECODE_SUBDIR = "measurements/decode"


def decode_records_dir() -> Path:
    return data_dir() / DECODE_SUBDIR


@dataclass(frozen=True)
class DecodeRecord:
    """A measured decode rate for one model on one machine under one runtime.

    Unlike :class:`SpecRecord` this is not written by a sweep. It accumulates
    from **production traffic**: every real request already moves
    `vllm:generation_tokens_total` and `vllm:request_decode_time_seconds_sum`,
    so the engine has been reporting its own rate all along and nothing read
    it. That matters for accuracy -- a synthetic sweep measures the workload
    somebody chose, while this measures the one that actually ran.

    Keyed like SpecRecord and for the identical reason: decode is
    bandwidth-bound, so the same model on a different part is a different
    number, and a vLLM release can change the kernels underneath it. A mismatch
    on any key component MISSES rather than approximating.

    ``context_band`` is the one key component SpecRecord has no analogue for,
    and it is the one the 61.5-vs-120.5 gap was hiding in. Decode reads the
    cache for the tokens actually present, so the rate at 512 tokens of context
    and the rate at 8192 are different measurements of a correctly-working
    engine. Banding by power of two keeps that honest without making every
    request its own record.
    """

    model_id: str
    gpu_name: str
    memory_bandwidth_gbps: float
    runtime_version: str
    #: Rounded DOWN to a power of two. See the class docstring.
    context_band: int
    #: How many sequences the engine was decoding concurrently, banded the same
    #: way: per-sequence decode slows as the batch grows, so a rate measured at
    #: 16 is not the rate at 1.
    concurrency_band: int

    #: The measurement itself, and the evidence behind it.
    decode_tps: float
    tokens: float
    decode_seconds: float
    requests: float
    #: What `fit.predict_decode_tps` said for the same deployment, so the two
    #: travel together and the ratio is never recomputed from a stale input.
    predicted_tps: float | None
    measured_at: float
    notes: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float | None:
        """Measured over predicted. None when there was no prediction to check.

        Above 1.0 means the gate is pessimistic, which is the safe direction for
        a gate and the wrong direction for a number on a card that says how fast
        this will be.
        """
        if not self.predicted_tps:
            return None
        return self.decode_tps / self.predicted_tps

    def key(self) -> str:
        return "|".join(
            (
                self.model_id,
                self.gpu_name,
                f"{self.memory_bandwidth_gbps:.0f}",
                self.runtime_version,
                str(self.context_band),
                str(self.concurrency_band),
            )
        )

    def filename(self) -> str:
        parts = (
            self.model_id,
            self.gpu_name,
            self.runtime_version,
            f"ctx{self.context_band}",
            f"seq{self.concurrency_band}",
        )
        return _UNSAFE.sub("-", "__".join(parts)).strip("-")[:180] + ".json"


def band(value: float) -> int:
    """Round down to a power of two, floored at 1.

    A band rather than the raw number because a record per distinct context
    length is a directory nobody can match against, and because the rate is a
    smooth function of it -- 3000 tokens and 3100 are the same measurement.
    """
    n = 1
    while n * 2 <= max(1.0, value):
        n *= 2
    return n


def save_decode(record: DecodeRecord, *, directory: Path | None = None) -> Path | None:
    """Write one decode record. Best effort, same as :func:`save`.

    Last writer wins within a key, deliberately. A newer window on the same
    model, hardware, runtime, context band and concurrency band is a better
    measurement of the same thing -- it has seen more recent kernels and more
    representative traffic -- and keeping a history here would be a time series
    the telemetry archive already owns.
    """
    target = directory or decode_records_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        path = target / record.filename()
        path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True))
        return path
    except OSError:
        log.warning("could not write the decode measurement", exc_info=True)
        return None


def load_decode_all(*, directory: Path | None = None) -> list[DecodeRecord]:
    """Every readable decode record. A corrupt file is skipped, never raised."""
    target = directory or decode_records_dir()
    if not target.is_dir():
        return []
    out: list[DecodeRecord] = []
    for path in sorted(target.glob("*.json")):
        try:
            out.append(DecodeRecord(**json.loads(path.read_text())))
        except (OSError, ValueError, TypeError):
            log.warning("skipping unreadable decode measurement %s", path, exc_info=True)
    return out


def matching_decode(
    model_id: str,
    *,
    gpu_name: str | None = None,
    memory_bandwidth_gbps: float | None = None,
    runtime_version: str | None = None,
    context_band: int | None = None,
    concurrency_band: int | None = None,
    directory: Path | None = None,
) -> list[DecodeRecord]:
    """Records for *model_id*, narrowed by whatever the caller can name.

    Every ``None`` criterion stops narrowing rather than excluding -- the same
    rule :func:`matching` follows. A caller that does not know the runtime
    version is asking a broader question, not an impossible one.

    Ordered best-match first: exact context band before near ones, because a
    rate measured at the context you are asking about is the answer and a rate
    measured at a different one is an estimate.
    """
    found = [r for r in load_decode_all(directory=directory) if r.model_id == model_id]
    if gpu_name is not None:
        found = [r for r in found if r.gpu_name == gpu_name]
    if memory_bandwidth_gbps is not None:
        found = [
            r
            for r in found
            if abs(r.memory_bandwidth_gbps - memory_bandwidth_gbps) < 1.0
        ]
    if runtime_version is not None:
        found = [r for r in found if r.runtime_version == runtime_version]
    if concurrency_band is not None:
        found = [r for r in found if r.concurrency_band == concurrency_band]
    if context_band is None:
        return sorted(found, key=lambda r: -r.measured_at)
    return sorted(
        found,
        key=lambda r: (abs((r.context_band or 1).bit_length() - context_band.bit_length()),
                       -r.measured_at),
    )


# ==========================================================================
# NCCL tuning: what this fabric's collectives actually cost, per node pair
# ==========================================================================

NCCL_SUBDIR = "measurements/nccl"


def nccl_records_dir() -> Path:
    return data_dir() / NCCL_SUBDIR


@dataclass(frozen=True)
class NcclRecord:
    """One collective, timed on one pair of machines, at one message size.

    The third record in this module and the same rule as the other two: a
    number measured somewhere else is not an answer here, so a mismatch on any
    key component MISSES rather than approximating.

    **The pair is the subject, not the cluster.** Nodes differ in NIC
    placement, PCIe slot, which rails are up and how the switch is wired, so a
    latency measured between A and B is not a fact about A and C. Sorted so the
    record is symmetric -- a collective has no direction and storing it twice
    would let the two copies disagree.

    **The image version is a key component** because the library is what is
    being measured. This estate has already been bitten twice by version
    confusion: the IB abort was reproduced against a HOST torch carrying NCCL
    2.28.9, the serving image LOADS 2.31.2, and `torch.cuda.nccl.version()`
    inside that image reports 2.29.7 because that is what torch was compiled
    against. Whichever number a record was taken under, a different one is a
    different measurement.

    **Size is banded, not stored raw**, for the reason `band()` gives: a
    5,760-byte all-reduce and an 8,192-byte one are the same measurement of the
    same regime, and a record per distinct size is a directory nobody can match
    against. The two regimes this estate actually generates are far enough
    apart that banding cannot blur them -- a decode all-reduce is KB and a
    prefill one is MB.
    """

    src: str
    dst: str
    #: What the LOADED libnccl reported, never what torch was compiled against.
    nccl_version: str
    #: The serving image this was measured under, since that is what carries
    #: the library.
    image: str
    #: Rounded DOWN to a power of two. See :func:`band`.
    size_band: int

    #: The measurement.
    microseconds: float
    busbw_gbps: float
    #: The environment this row was taken under -- `{}` for the default, or the
    #: single knob being tried. Stored so a "best" is traceable to the setting
    #: that produced it rather than being an unattributable number.
    env: dict = field(default_factory=dict)
    measured_at: float = 0.0
    #: Set when the collective did not complete. A record of a FAILURE is worth
    #: keeping: "this pair aborts at 4 MiB" is a fact about the fabric, and the
    #: absence of a record is indistinguishable from nobody having tried.
    error: str | None = None

    def key(self) -> tuple:
        """What makes two records the same measurement."""
        return (
            *sorted((self.src, self.dst)),
            self.nccl_version,
            self.image,
            self.size_band,
            tuple(sorted(self.env.items())),
        )

    def filename(self) -> str:
        a, b = sorted((self.src, self.dst))
        env = "-".join(f"{k}={v}" for k, v in sorted(self.env.items())) or "default"
        parts = (a, b, self.nccl_version, self.image, f"n{self.size_band}", env)
        return _UNSAFE.sub("-", "__".join(parts)).strip("-")[:180] + ".json"


def save_nccl(record: NcclRecord, *, directory: Path | None = None) -> Path | None:
    """Write one NCCL record. Best effort, like the other two savers.

    Last writer wins within a key: a newer run on the same pair, library, size
    band and environment is a better measurement of the same thing.
    """
    target = directory or nccl_records_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        path = target / record.filename()
        path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True))
        return path
    except OSError:
        log.warning("could not write the nccl measurement", exc_info=True)
        return None


def load_nccl_all(*, directory: Path | None = None) -> list[NcclRecord]:
    """Every readable NCCL record. A corrupt file is skipped, never raised."""
    target = directory or nccl_records_dir()
    if not target.is_dir():
        return []
    out: list[NcclRecord] = []
    for path in sorted(target.glob("*.json")):
        try:
            out.append(NcclRecord(**json.loads(path.read_text())))
        except Exception:
            log.warning("skipping unreadable nccl record %s", path, exc_info=True)
    return out


def matching_nccl(
    src: str,
    dst: str,
    *,
    nccl_version: str | None = None,
    image: str | None = None,
    size_band: int | None = None,
    directory: Path | None = None,
) -> list[NcclRecord]:
    """Records for this PAIR, narrowed by whatever the caller can name.

    Symmetric in the pair, because a collective has no direction. Every
    ``None`` criterion stops narrowing rather than excluding, the same rule
    :func:`matching` and :func:`matching_decode` follow.

    Newest first. Failures are included and carry `error`: a caller deciding
    whether to tune needs to know the difference between "never tried" and
    "tried and the fabric refused".
    """
    want = sorted((src, dst))
    found = [r for r in load_nccl_all(directory=directory) if sorted((r.src, r.dst)) == want]
    if nccl_version is not None:
        found = [r for r in found if r.nccl_version == nccl_version]
    if image is not None:
        found = [r for r in found if r.image == image]
    if size_band is not None:
        found = [r for r in found if r.size_band == size_band]
    return sorted(found, key=lambda r: -r.measured_at)


def best_nccl_env(
    src: str,
    dst: str,
    size_bytes: float,
    *,
    nccl_version: str | None = None,
    image: str | None = None,
    directory: Path | None = None,
) -> dict:
    """The environment that measured fastest for this pair at this size.

    `{}` when nothing was measured, which is the ordinary answer and is NOT a
    recommendation to use the defaults -- it means nobody has looked. The
    caller renders nothing in that case, which is what an untuned launch has
    always done.

    **Per size, deliberately.** The whole reason this record is banded is that
    the answer differs between the KB all-reduce a decode step issues dozens of
    times per token and the MB one prefill moves. Asking for "the best setting
    for this link" without naming a size is asking a question with two answers.
    """
    rows = [
        r
        for r in matching_nccl(
            src, dst, nccl_version=nccl_version, image=image,
            size_band=band(size_bytes), directory=directory,
        )
        if r.error is None and r.microseconds > 0
    ]
    if not rows:
        return {}
    return dict(min(rows, key=lambda r: r.microseconds).env)


#: The all-reduce a TENSOR-PARALLEL DECODE step issues, dozens of times per
#: token. `hidden_size * 2 bytes * batch`, so 5,760 B for gpt-oss-120b and
#: 8,192 B for DeepSeek-V4-Flash at batch 1. The regime that dominates
#: interactive serving.
DECODE_COLLECTIVE_BYTES = 8192
#: What prefill and weight movement look like: megabytes, bandwidth-bound.
#: The opposite regime, and the reason one setting cannot simply be "best".
BULK_COLLECTIVE_BYTES = 4 << 20


def tuning_env(
    src: str,
    dst: str,
    *,
    image: str | None = None,
    nccl_version: str | None = None,
    prefer: str = "balanced",
    directory: Path | None = None,
) -> dict:
    """The NCCL settings measured best for this PAIR, or `{}`.

    A launch gets ONE environment and its collectives span four decades of
    message size, so "best" has to mean best somewhere without being worse
    somewhere else. Measured on this estate 2026-09-11, the two regimes
    genuinely disagree: `NCCL_PROTO=LL` is fine at 5,760 B (16.3 us) and
    catastrophic at 4 MiB (1.29 GB/s against 8.09 by default). Shipping the
    small-message winner would have wrecked prefill.

    So the rule is **best in bulk, no regression at decode**:

    * rank candidates by bulk bandwidth, where the spread is large and the
      measurement is stable;
    * drop any whose decode time is worse than the default's;
    * `{}` when nothing survives, which leaves the launch exactly as it was.

    `{}` is also the answer when the pair, the image or the library version
    has no record. That is the ordinary case and it is not a gap: an untuned
    launch is what every launch has always been, and a value measured on a
    different pair is not a fact about this one.
    """
    rows = [
        r
        for r in matching_nccl(
            src, dst, image=image, nccl_version=nccl_version, directory=directory
        )
        if r.error is None and r.microseconds > 0
    ]
    if not rows:
        return {}

    def at(size: int) -> dict:
        band_ = band(size)
        out: dict[tuple, NcclRecord] = {}
        for r in rows:
            if r.size_band != band_:
                continue
            key = tuple(sorted(r.env.items()))
            # Newest wins within an env, the same rule `save_nccl` follows.
            if key not in out or r.measured_at > out[key].measured_at:
                out[key] = r
        return out

    bulk, decode = at(BULK_COLLECTIVE_BYTES), at(DECODE_COLLECTIVE_BYTES)
    if not bulk:
        return {}
    default = bulk.get(())
    if default is None:
        # No baseline to be "no worse than". Refuse rather than crown a
        # candidate that nothing was compared against.
        return {}

    decode_default = decode.get(())
    # `prefer="bulk"` drops the no-regression rule, and only measured traffic
    # justifies that. It is what `preference_for` returns when a served model's
    # own tokens arrive overwhelmingly as prompt -- at which point the decode
    # collective is a rounding error in its step time and the bandwidth one is
    # the whole cost. Absent that evidence the rule stands, because a decode
    # regression on an interactive deployment is paid dozens of times per token.
    guard_decode = prefer != "bulk"

    best, best_bw = {}, default.busbw_gbps
    for key, rec in bulk.items():
        if rec.busbw_gbps <= best_bw:
            continue
        if guard_decode and decode_default is not None:
            same = decode.get(key)
            # Unmeasured at the decode size is not "fine at the decode size".
            if same is None or same.microseconds > decode_default.microseconds:
                continue
        best, best_bw = dict(rec.env), rec.busbw_gbps
    return best


# ==========================================================================
# Observed traffic shape: what KIND of work a deployment actually does
# ==========================================================================

WORKLOAD_SUBDIR = "measurements/workload"

#: Above this share of tokens arriving as prompt, a deployment's collectives
#: are dominated by the megabyte prefill all-reduce rather than the kilobyte
#: decode one, and it is worth trading decode latency for bulk bandwidth.
#:
#: 0.8 rather than 0.5 because the trade is asymmetric and the asymmetry is
#: measured: on this estate the bulk winner buys ~5% more bandwidth over the
#: balanced choice and costs 24% on decode. A deployment has to be decisively
#: prefill-heavy before that is worth taking, and one that is merely
#: prompt-leaning should keep the setting that regresses nothing.
PREFILL_DOMINANT_SHARE = 0.8


def workload_records_dir() -> Path:
    return data_dir() / WORKLOAD_SUBDIR


@dataclass(frozen=True)
class WorkloadRecord:
    """What one served model's traffic actually looked like.

    Accumulates from PRODUCTION traffic, like :class:`DecodeRecord` and for the
    same reason: a synthetic sweep measures the workload somebody chose, and
    this measures the one that ran. `vllm:prompt_tokens_total` and
    `vllm:generation_tokens_total` have been moving all along.

    Keyed on the served name alone, deliberately -- unlike every other record
    in this module. The others describe how a machine behaves and are
    invalidated by hardware and versions; this describes how PEOPLE USE a
    model, which survives a hardware change and a vLLM upgrade untouched. What
    would invalidate it is the traffic changing, and the record is rewritten on
    every window for exactly that reason.
    """

    served_name: str
    #: Share of tokens arriving as prompt. See `EngineLoad.prefill_share`.
    prefill_share: float
    prompt_tokens: float
    generation_tokens: float
    requests: float = 0.0
    measured_at: float = 0.0

    def filename(self) -> str:
        return _UNSAFE.sub("-", self.served_name).strip("-")[:180] + ".json"


def save_workload(record: WorkloadRecord, *, directory: Path | None = None) -> Path | None:
    target = directory or workload_records_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        path = target / record.filename()
        path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True))
        return path
    except OSError:
        log.warning("could not write the workload record", exc_info=True)
        return None


def load_workload(
    served_name: str, *, directory: Path | None = None
) -> WorkloadRecord | None:
    """This served model's observed traffic shape, or None if nothing ran yet.

    None is the ordinary answer for a model being launched for the first time,
    and it means "tune for neither regime in particular" rather than "tune for
    decode".
    """
    target = directory or workload_records_dir()
    path = target / (_UNSAFE.sub("-", served_name).strip("-")[:180] + ".json")
    try:
        return WorkloadRecord(**json.loads(path.read_text()))
    except Exception:
        return None


def preference_for(
    served_name: str, *, directory: Path | None = None
) -> str:
    """`"bulk"` when this model's own traffic is prefill-dominated, else
    `"balanced"`.

    Never `"decode"`: the balanced rule already refuses a decode regression, so
    there is nothing a decode preference would additionally buy. The only
    choice worth making is whether to ALLOW that regression, and only measured
    traffic can justify it.
    """
    record = load_workload(served_name, directory=directory)
    if record is None:
        return "balanced"
    return "bulk" if record.prefill_share >= PREFILL_DOMINANT_SHARE else "balanced"
