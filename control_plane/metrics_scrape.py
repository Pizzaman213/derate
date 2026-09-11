"""Read a runtime's own Prometheus counters, standard library only.

The gateway measures decode throughput end to end (``gateway/stats.py``) and
the routing score prefers those numbers over any estimate. What it has never
read is what the *engine* knows about itself, and for speculative decoding that
is the whole question: vLLM counts every drafted token and every accepted one,
and derate was reporting a floor-to-ceiling range because nothing collected it.

Standard library only, and no ``prometheus_client``, for the reason
``redaction.py`` gives for the same choice: this runs wherever a backend is
reachable, including a worker whose image does not carry the dependency.

**Names are matched by prefix, never by a fixed list.** The pinned image
(vLLM 0.28.1rc1.dev462) exports four series under ``vllm:spec_decode_``, and
which four has changed between releases -- vLLM's own dashboard query in
``v1/spec_decode/metrics.py`` spells one of them with ``_total`` and another
without, in the same file. Matching a prefix and tolerating the suffix is the
same move ``imageprobe.py`` makes for the architecture table: ask the thing
itself rather than keeping a copy of its answer.

**Counters are cumulative, so a measurement is a difference.** Reading once
gives you the engine's whole life since it started, which for a sweep that
drives three workloads in sequence would blend all three. :func:`delta` is how
a window is taken, and every caller here takes one.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

#: Every series this module reads. A shorter prefix than the metric names so a
#: release that renames the tail is still found.
_PREFIX = "vllm:spec_decode_"

#: The GPU prefix cache's own two counters. Deliberately NOT `vllm:prefix_` --
#: the pinned image also exports `vllm:external_prefix_cache_*`, which is the
#: KV offloading tier and a different fact about a different store. Anchoring
#: on `vllm:prefix_cache_` excludes it because those series begin
#: `vllm:external_`, and mixing the two would report an offload hit as a
#: prefix-cache hit.
_CACHE_PREFIX = "vllm:prefix_cache_"

#: The engine's own view of its load. Read by exact name rather than by prefix,
#: because unlike the two families above these are unrelated series that happen
#: to answer one question together, and a prefix wide enough to catch them all
#: would also catch things that mean something else.
#:
#: `kv_cache_usage_perc` was `gpu_cache_usage_perc` in older vLLM. Both are
#: accepted: the name moved, the meaning did not, and a deployment pinned to an
#: older image should not silently report nothing.
_KV_USAGE_NAMES = (
    "vllm:kv_cache_usage_perc",
    "vllm:gpu_cache_usage_perc",
)
_RUNNING = "vllm:num_requests_running"
_WAITING = "vllm:num_requests_waiting"
_PREEMPTIONS = "vllm:num_preemptions_total"
_GENERATION_TOKENS = "vllm:generation_tokens_total"
_PROMPT_TOKENS = "vllm:prompt_tokens_total"
_DECODE_SUM = "vllm:request_decode_time_seconds_sum"
_DECODE_COUNT = "vllm:request_decode_time_seconds_count"

#: `name{label="value",...} 12.0` -- the Prometheus text exposition format,
#: reduced to the two parts anything here needs. Deliberately not a general
#: parser: exemplars, timestamps and `# HELP`/`# TYPE` lines are skipped by the
#: caller rather than modelled, because a partial parser that is honest about
#: its scope beats a complete one nobody checks.
_SAMPLE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{[^}]*\})?\s+(?P<value>[^\s]+)")
_LABEL = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:[^"\\]|\\.)*)"')


@dataclass(frozen=True)
class Sample:
    name: str
    labels: dict[str, str]
    value: float


@dataclass
class SpecDecode:
    """What the engine counted about its own speculation.

    Every field is cumulative since the engine started. Take a
    :func:`delta` before reading anything as a rate.
    """

    #: Draft rounds. One per verify step that speculated.
    drafts: float = 0.0
    #: Tokens proposed across every round.
    draft_tokens: float = 0.0
    #: Tokens the target model then accepted.
    accepted_tokens: float = 0.0
    #: Accepted count per draft POSITION, 0-indexed, from
    #: `vllm:spec_decode_num_accepted_tokens_per_pos`. Empty when the image
    #: does not export it.
    accepted_per_pos: list[float] = field(default_factory=list)

    @property
    def basis(self) -> str:
        """Which tier of evidence this carries, so a caller can say so.

        ``per_pos`` is the one worth having: it makes the best drafted-token
        count solvable from a single run, because the acceptance at every
        position up to k is already in hand. ``aggregate`` gives one mean and
        forces a relaunch per k. ``none`` means the engine speculated not at
        all, or was never asked to.
        """
        if self.accepted_per_pos:
            return "per_pos"
        if self.drafts > 0:
            return "aggregate"
        return "none"

    @property
    def mean_acceptance(self) -> float | None:
        """Accepted over proposed. ``None`` when nothing was drafted.

        Not the same question as :meth:`acceptance_at`, and the difference is
        the reason both exist: this is one number over every position, which
        is what you get when the per-position series is missing.
        """
        if self.draft_tokens <= 0:
            return None
        return self.accepted_tokens / self.draft_tokens

    def acceptance_at(self, position: int) -> float | None:
        """Fraction of draft rounds in which *position* was accepted.

        This is vLLM's own definition -- its dashboard divides
        ``num_accepted_tokens_per_pos`` by ``num_drafts`` -- and it is a
        CUMULATIVE probability, not a conditional one: a draft is only checked
        at position 2 if position 1 was accepted first. That is exactly the
        term wanted when summing expected accepted length, and it means the
        sum needs no assumption that positions are independent, which they are
        not.
        """
        if self.drafts <= 0 or position >= len(self.accepted_per_pos):
            return None
        return self.accepted_per_pos[position] / self.drafts

    def expected_accepted(self, k: int) -> float | None:
        """Drafted tokens settled per step at *k*, measured.

        The sum of the cumulative per-position acceptances, which is the
        expected length of the accepted run. Add one for the bonus token the
        target emits whether or not anything was accepted.
        """
        if self.drafts <= 0 or not self.accepted_per_pos:
            return None
        k = max(0, min(int(k), len(self.accepted_per_pos)))
        return sum(self.accepted_per_pos[:k]) / self.drafts

    @property
    def consistent(self) -> bool:
        """Whether the per-position series sums to the aggregate count.

        Every accepted token is accepted at exactly one position, so these
        must agree. They will not if a release changes what either counts, and
        a caller that trusted one over the other would report a rate that is
        quietly wrong rather than obviously broken.
        """
        if not self.accepted_per_pos:
            return True
        return abs(sum(self.accepted_per_pos) - self.accepted_tokens) <= 1.0


@dataclass
class PrefixCache:
    """What the engine counted about its own KV prefix cache.

    Both fields are cumulative since the engine started, the same contract
    :class:`SpecDecode` carries: read once and you have the engine's whole
    life, which for a "hit rate right now" panel is the wrong window. Take a
    :func:`cache_delta` first.
    """

    #: Tokens looked up, NOT requests and not blocks -- the image's own HELP
    #: text reads "in terms of number of queried tokens".
    #: `vllm:prefix_cache_queries_total`.
    queries: float = 0.0
    #: Tokens that were already cached. `vllm:prefix_cache_hits_total`.
    hits: float = 0.0

    @property
    def hit_rate(self) -> float | None:
        """Hits over queries, or None when nothing was asked.

        None rather than 0.0, and the distinction is the whole point: an engine
        that served no tokens in this window has no hit rate, while one that
        missed on every token has a real, measured 0.0. Reporting the first as
        a zero puts a confident number on a screen where nothing was measured.
        """
        if self.queries <= 0.0:
            return None
        return self.hits / self.queries


def parse(text: str) -> list[Sample]:
    """Every sample in a Prometheus exposition body. Comments skipped."""
    out: list[Sample] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if match is None:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue  # NaN, +Inf and anything else non-numeric are not counts
        raw = match.group("labels") or ""
        labels = {m.group("key"): m.group("value") for m in _LABEL.finditer(raw)}
        out.append(Sample(match.group("name"), labels, value))
    return out


def _tail(name: str, prefix: str = _PREFIX) -> str:
    """The metric name past *prefix*, with Prometheus' counter suffix off.

    `prometheus_client` appends `_total` to every Counter on the wire, and
    vLLM's own dashboard queries spell these both ways. Normalising here means
    neither spelling is a miss.

    It deliberately does NOT strip `_created`. That suffix is not a spelling of
    the same counter: `prometheus_client` emits one alongside every Counter
    carrying the unix time the series was born, so
    `vllm:prefix_cache_queries_created` reads 1.79e9 next to a queries count of
    34845. Left un-normalised, it fails the exact-name tests below and is
    dropped -- which is the point. A reader that stripped it would add a
    timestamp into a cache-hit rate.
    """
    tail = name[len(prefix):]
    return tail[: -len("_total")] if tail.endswith("_total") else tail


def spec_decode(text: str) -> SpecDecode:
    """The speculation counters out of one exposition body.

    Sums across engine ranks rather than picking one: a tensor-parallel launch
    exports a series per engine index, and the acceptance of rank 0 is not the
    acceptance of the deployment.
    """
    found = SpecDecode()
    by_pos: dict[int, float] = {}
    for sample in parse(text):
        if not sample.name.startswith(_PREFIX):
            continue
        tail = _tail(sample.name, _PREFIX)
        if tail == "num_drafts":
            found.drafts += sample.value
        elif tail == "num_draft_tokens":
            found.draft_tokens += sample.value
        elif tail == "num_accepted_tokens":
            found.accepted_tokens += sample.value
        elif tail == "num_accepted_tokens_per_pos":
            try:
                position = int(sample.labels.get("position", ""))
            except ValueError:
                continue
            by_pos[position] = by_pos.get(position, 0.0) + sample.value
    if by_pos:
        # Dense and 0-indexed, so `accepted_per_pos[i]` is position i even if a
        # position never fired and its series is absent.
        found.accepted_per_pos = [by_pos.get(i, 0.0) for i in range(max(by_pos) + 1)]
    return found


def delta(before: SpecDecode, after: SpecDecode) -> SpecDecode:
    """*after* minus *before*, which is the only form that means a rate.

    Clamped at zero per field: an engine that restarted between the two reads
    resets its counters, and a negative count would otherwise travel into an
    acceptance rate as a number no reader could interpret.
    """
    width = max(len(before.accepted_per_pos), len(after.accepted_per_pos))

    def at(source: SpecDecode, i: int) -> float:
        return source.accepted_per_pos[i] if i < len(source.accepted_per_pos) else 0.0

    return SpecDecode(
        drafts=max(0.0, after.drafts - before.drafts),
        draft_tokens=max(0.0, after.draft_tokens - before.draft_tokens),
        accepted_tokens=max(0.0, after.accepted_tokens - before.accepted_tokens),
        accepted_per_pos=[max(0.0, at(after, i) - at(before, i)) for i in range(width)],
    )


def prefix_cache(text: str) -> PrefixCache:
    """The prefix-cache counters out of one exposition body.

    Summed across engine ranks for the same reason :func:`spec_decode` sums:
    a tensor-parallel launch exports a series per engine index, and rank 0's
    cache is not the deployment's cache.
    """
    found = PrefixCache()
    for sample in parse(text):
        if not sample.name.startswith(_CACHE_PREFIX):
            continue
        tail = _tail(sample.name, _CACHE_PREFIX)
        if tail == "queries":
            found.queries += sample.value
        elif tail == "hits":
            found.hits += sample.value
    return found


def cache_delta(before: PrefixCache, after: PrefixCache) -> PrefixCache:
    """*after* minus *before*, clamped at zero per field.

    Same clamp and same reason as :func:`delta`: a backend that restarted
    between the two reads resets its counters, and a negative query count
    would travel into a hit rate as a number no reader could interpret.
    """
    return PrefixCache(
        queries=max(0.0, after.queries - before.queries),
        hits=max(0.0, after.hits - before.hits),
    )


@dataclass
class EngineLoad:
    """What the engine says about its own memory and queue, right now.

    Mixed on purpose, and the mixture is the one thing to be careful with:
    ``kv_cache_usage``, ``requests_running`` and ``requests_waiting`` are
    GAUGES -- an instantaneous reading that means nothing differenced -- while
    ``preemptions``, ``generation_tokens`` and the two decode fields are
    CUMULATIVE counters that mean nothing *un*-differenced. :func:`load_delta`
    handles each correctly; do not subtract this dataclass by hand.

    The gauges are ``None`` when the engine did not export them, never 0.0. An
    engine holding no cache and an engine that was never asked are different
    facts, and this package has refused to collapse that distinction since
    :attr:`PrefixCache.hit_rate`.
    """

    #: Fraction of the KV cache in use, 0..1. **Not a percentage**, whatever the
    #: series name says -- the image's own HELP reads "1 means 100 percent
    #: usage". Reporting it as a percent would be wrong by 100x on the one
    #: number the fit gate exists to predict.
    kv_cache_usage: float | None = None
    requests_running: float | None = None
    requests_waiting: float | None = None
    #: Cumulative. A preemption is a running request evicted because the KV
    #: cache ran out -- exactly the failure the fit gate is there to prevent,
    #: and until now the one thing it could not see happen.
    preemptions: float = 0.0
    #: Cumulative tokens generated, and the engine's own time-in-decode. Their
    #: ratio is a MEASURED decode rate, which is the only number here that can
    #: be compared against ``predict_decode_tps``.
    generation_tokens: float = 0.0
    decode_time_s: float = 0.0
    decode_count: float = 0.0
    #: Cumulative prompt tokens. With ``generation_tokens`` and
    #: ``decode_count`` this gives the mean sequence length behind a window,
    #: which is what a measured decode rate has to be filed under -- decode
    #: reads the cache for the tokens actually present, so the same engine is
    #: genuinely faster at 300 tokens of context than at 8192.
    prompt_tokens: float = 0.0

    @property
    def mean_sequence_tokens(self) -> float | None:
        """Average prompt + generation per request in this window, or None."""
        if self.decode_count <= 0.0:
            return None
        return (self.prompt_tokens + self.generation_tokens) / self.decode_count

    @property
    def prefill_share(self) -> float | None:
        """Share of this window's tokens that arrived as PROMPT, or None.

        The one honest answer to "what kind of traffic is this". A collective's
        cost depends on message size and the two regimes are four decades
        apart: prefill all-reduces the whole prompt at once (megabytes), decode
        all-reduces one position (kilobytes). So a deployment serving 8k-token
        prompts for 50-token answers is a bandwidth workload wearing a serving
        workload's clothes, and one answering long-form from short prompts is
        the opposite -- and nothing about the DEPLOYMENT says which, because
        `context_length` is a capacity and not a workload.

        None when the window saw no tokens. An idle engine has no traffic
        shape, and 0.0 would read as "pure decode" and tune for it.
        """
        total = self.prompt_tokens + self.generation_tokens
        if total <= 0.0:
            return None
        return self.prompt_tokens / total

    @property
    def decode_tps(self) -> float | None:
        """Measured decode tokens per second, or None when nothing decoded.

        ``generation_tokens`` counts every token the engine produced, including
        each request's first -- and the first token comes out of PREFILL, not
        decode, so it is not inside ``request_decode_time_seconds``. Subtracting
        one per request is what makes the numerator and the denominator describe
        the same window. On a short request that correction is most of the
        error: four requests of 231 tokens is 924 against 920, but forty
        requests of ten tokens is 400 against 360.

        None, not 0.0, when no request finished in the window -- an idle engine
        has no decode rate, and a fabricated zero here would drag any average
        that included it straight down.
        """
        if self.decode_time_s <= 0.0:
            return None
        tokens = self.generation_tokens - self.decode_count
        if tokens <= 0.0:
            return None
        return tokens / self.decode_time_s


def engine_load(text: str) -> EngineLoad:
    """The engine's load counters out of one exposition body.

    Counters sum across engine ranks, exactly as :func:`spec_decode` does and
    for the same reason. The gauges do NOT sum: ``kv_cache_usage`` is a
    fraction, and adding rank 0's 0.4 to rank 1's 0.4 would report a cache at
    80% that is at 40%. Tensor-parallel ranks each hold a shard of one logical
    cache, so the honest aggregate is the fullest rank -- it is the one that
    will preempt first, and preemption is what the number is for.
    """
    found = EngineLoad()
    usage: list[float] = []
    running: list[float] = []
    waiting: list[float] = []
    for sample in parse(text):
        name = sample.name
        if name in _KV_USAGE_NAMES:
            usage.append(sample.value)
        elif name == _RUNNING:
            running.append(sample.value)
        elif name == _WAITING:
            waiting.append(sample.value)
        elif name == _PREEMPTIONS:
            found.preemptions += sample.value
        elif name == _GENERATION_TOKENS:
            found.generation_tokens += sample.value
        elif name == _PROMPT_TOKENS:
            found.prompt_tokens += sample.value
        elif name == _DECODE_SUM:
            found.decode_time_s += sample.value
        elif name == _DECODE_COUNT:
            found.decode_count += sample.value
    found.kv_cache_usage = max(usage) if usage else None
    found.requests_running = sum(running) if running else None
    found.requests_waiting = sum(waiting) if waiting else None
    return found


def load_delta(before: EngineLoad, after: EngineLoad) -> EngineLoad:
    """*after* minus *before* for the counters; *after* alone for the gauges.

    Clamped at zero per counter, same as :func:`delta`: an engine that restarted
    between the two reads has reset them, and a negative token count would
    travel into a decode rate as a number no reader could interpret.
    """
    return EngineLoad(
        kv_cache_usage=after.kv_cache_usage,
        requests_running=after.requests_running,
        requests_waiting=after.requests_waiting,
        preemptions=max(0.0, after.preemptions - before.preemptions),
        generation_tokens=max(0.0, after.generation_tokens - before.generation_tokens),
        decode_time_s=max(0.0, after.decode_time_s - before.decode_time_s),
        decode_count=max(0.0, after.decode_count - before.decode_count),
        prompt_tokens=max(0.0, after.prompt_tokens - before.prompt_tokens),
    )


def fetch(base_url: str, *, timeout: float = 5.0) -> str | None:
    """A backend's `/metrics` body, or None when it could not be read.

    Degrades rather than raises, the same contract as ``imageprobe.probe`` and
    the fit gate's live-memory kwarg: the better number is used when it is
    there and its absence is never a failure. A runtime with metrics turned
    off, an unreachable host and a 404 are all "no reading" here -- what to do
    about that is the caller's decision, and every caller in this repo says so
    on screen rather than substituting a number.

    *base_url* is a deployment's ``backend_url``, which ends in ``/v1``;
    ``/metrics`` is a sibling of it, not a child.
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        with urllib.request.urlopen(root + "/metrics", timeout=timeout) as response:
            if response.status != 200:
                return None
            return response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def read(base_url: str, *, timeout: float = 5.0) -> SpecDecode | None:
    """:func:`fetch` then :func:`spec_decode`. None when nothing answered."""
    body = fetch(base_url, timeout=timeout)
    return None if body is None else spec_decode(body)


def read_engine_load(base_url: str, *, timeout: float = 5.0) -> EngineLoad | None:
    """One scrape, parsed for load. None when the backend could not be read."""
    text = fetch(base_url, timeout=timeout)
    return None if text is None else engine_load(text)


def read_prefix_cache(base_url: str, *, timeout: float = 5.0) -> PrefixCache | None:
    """:func:`fetch` then :func:`prefix_cache`. None when nothing answered."""
    body = fetch(base_url, timeout=timeout)
    return None if body is None else prefix_cache(body)
