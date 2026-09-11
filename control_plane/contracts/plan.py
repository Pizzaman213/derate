"""ParallelismPlan and FitResult. 00-architecture.md section 4.3.

Day-0 file. Transcribed from the architecture doc, not designed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .model import ModelShape


class ParallelismKind(str, Enum):
    SINGLE_NODE = "single_node"
    TENSOR = "tensor"
    PIPELINE = "pipeline"
    EXPERT = "expert"
    HYBRID = "hybrid"


class SpeculativeMethod(str, Enum):
    """How a runtime drafts the tokens it then verifies in one step.

    The vocabulary is fixed here; which members are *available* for a given
    model is not a contract question at all -- the checkpoint's config decides
    (``resolver/speculators.py``) and the runtime image decides
    (``resolver/imageprobe.py``). These spellings are also exactly what
    ``vllm serve --speculative-config`` calls them, so nothing downstream
    translates between two names for the same three things.
    """

    #: A multi-token-prediction module the checkpoint carries and no runtime
    #: loads by default. Declared by ``num_nextn_predict_layers``.
    MTP = "mtp"
    #: DeepSeek-V4's own speculator, declared by the ``dspark_*`` keys.
    DSPARK = "dspark"
    #: Drafts by matching recent output against the prompt. Needs no model
    #: support and loads no weights, so it is available for every model.
    NGRAM = "ngram"
    #: A separately-published draft head, trained against one target model and
    #: shipped in its own repository. Unlike the three above, these are not
    #: declared by the target's config at all -- the operator names the head and
    #: ``SpeculativeSpec.model`` carries it.
    EAGLE = "eagle"
    EAGLE3 = "eagle3"
    MEDUSA = "medusa"
    #: DFlash. Read out of the pinned image's own `config/speculative.py`,
    #: where `DFlashModelTypes = Literal["dflash"]` -- the method name is
    #: `dflash` even though the classes are `DFlash2DraftModel`,
    #: `DFlashLagunaForCausalLM` and friends.
    DFLASH = "dflash"


@dataclass(frozen=True)
class SpeculativeSpec:
    """Speculative decoding, as an input to the fit gate and the launch.

    Both cost fields are plain ints rather than optionals on purpose: this type
    is what a *launch* carries, and derate does not launch what it cannot
    budget. A method whose draft cost could not be derived -- DSpark today --
    is reported to the screen by ``resolver/speculators.py`` and never reaches
    here.

    ``draft_bytes`` is the memory charge and ``draft_params`` the bandwidth
    one, and they are not interchangeable: the first comes from the measured
    shards where a measurement exists, the second is a parameter count. Both
    are zero for ngram, which is a derived zero rather than a missing figure.
    """

    method: SpeculativeMethod
    #: Tokens drafted per verify step. One step reads the weights once and
    #: settles up to ``num_speculative_tokens + 1`` positions.
    num_speculative_tokens: int
    draft_bytes: int
    draft_params: int
    #: The head's own repository, for a method whose draft ships separately from
    #: the target. ``None`` for the methods the target's own checkpoint carries
    #: (mtp, dspark) and for ngram, which has no weights at all.
    #:
    #: Trailing and defaulted so every construction that predates external heads
    #: is unchanged, and typed as a plain id because that is what reaches the
    #: runtime: it is substituted into ``--speculative-config``'s ``model``
    #: field and therefore passes the same command-safety grammar a model_id
    #: does (deploy/recipes.py::_check_command_safe).
    model: str | None = None
    #: The draft's own KV cache as a fraction of the target's, at the same
    #: context. Zero for ngram (no model) and for an in-checkpoint MTP module,
    #: whose cache vLLM allocates inside the target's own budget.
    #:
    #: This exists because leaving it out killed real launches. A separately
    #: loaded head is a transformer with its own layers and its own cache, and
    #: charging only the drafted positions under-budgeted a DSpark launch by
    #: exactly the head's share: derate handed vLLM 1.17 GiB and the engine
    #: refused to start, saying it needed 1.37 for one request at 8192. The
    #: head was 5 layers against the target's 36 -- 5/36 of 1.17 is 0.16, and
    #: 1.17 + 0.16 is what it was asking for.
    draft_kv_ratio: float = 0.0


@dataclass(frozen=True)
class ParallelismPlan:
    kind: ParallelismKind
    tensor_parallel: int
    pipeline_parallel: int
    expert_parallel: int
    data_parallel: int
    node_ids: list[str]
    reason: str  # one sentence, shown verbatim in the UI
    measured_link_gbps: float  # what the decision was based on
    rejected: list[str]  # e.g. ["TP=2: link 10.2 GB/s below 40 GB/s threshold"]

    @property
    def world_size(self) -> int:
        return self.tensor_parallel * self.pipeline_parallel * self.data_parallel


class Verdict(str, Enum):
    FITS = "fits"
    FITS_DEGRADED = "fits_degraded"  # loads, but predicted decode is unusably slow
    WONT_FIT = "wont_fit"


@dataclass
class MemoryBreakdown:
    weights: int
    kv_cache: int
    activations: int
    comm_buffers: int
    replicated: int
    framework_overhead: int

    @property
    def total(self) -> int:
        return (
            self.weights
            + self.kv_cache
            + self.activations
            + self.comm_buffers
            + self.replicated
            + self.framework_overhead
        )


@dataclass
class FitResult:
    verdict: Verdict
    breakdown: MemoryBreakdown
    usable_per_node: int
    headroom: int
    reason: str
    limiting_term: str  # "weights"|"kv_cache"|"bandwidth"|"combined"|"context"
    max_context_that_fits: int | None
    predicted_decode_tps: float | None
    warnings: list[str] = field(default_factory=list)
    # Which budget ``usable_per_node`` came from: "static" is the addressable
    # ceiling under the guardrail -- what this hardware could ever spend --
    # and "live" is what the node could actually hand out at the moment the
    # check ran. Trailing and defaulted, so every existing construction is
    # unchanged. It exists because Deployment.fit is persisted and
    # _emit_fit_miss reports usable_per_node from it: without this a stored
    # verdict cannot say which budget judged it.
    budget_basis: str = "static"
    # What speculative decoding would do to decode throughput, as the two ends
    # of a range and never a single number. The ceiling is every drafted token
    # accepted; the floor is none accepted and the draft head read for nothing,
    # which is genuinely SLOWER than ``predicted_decode_tps`` and has to be
    # shown as such. Nothing here estimates an acceptance rate, so nothing here
    # can say where between them a real workload lands -- see
    # ``fit/calculator.py::speculative_decode_tps_range``.
    #
    # All three stay None/empty unless ``FitRequest.speculative`` was set.
    speculative_decode_tps_floor: float | None = None
    speculative_decode_tps_ceiling: float | None = None
    # One sentence, rendered verbatim on the same terms as ``reason``.
    speculative_reason: str = ""
    # The other end of the decode range: the rate with an EMPTY cache.
    #
    # ``predicted_decode_tps`` is computed against the cache for a sequence at
    # the full requested context, so it answers "the rate once the context is
    # full" -- the slowest the model will ever decode. Decoding reads the
    # tokens actually present, so a short request is genuinely faster, and on
    # measured hardware the gap is large: 61.5 against a measured 120.3 for
    # Qwen3-0.6B at 8192. One number cannot be both, so the card states both.
    #
    # Trailing and defaulted, so every existing construction is unchanged, and
    # deliberately NOT read by the degraded threshold, by routing strength, or
    # by any list view: those keep the conservative end. See
    # ``fit/calculator.py`` and ``head_scan.py``, which already computes this
    # exact figure as its scan baseline.
    predicted_decode_tps_empty: float | None = None

    @property
    def ok(self) -> bool:
        return self.verdict is not Verdict.WONT_FIT


@dataclass(frozen=True)
class FitRequest:
    shape: ModelShape
    context_length: int
    max_concurrent_seqs: int
    kv_dtype: str
    plan: ParallelismPlan
    # Measured on-disk weight bytes from the resolver's safetensors/GGUF
    # accounting; when present the fit calculator must prefer it over
    # total_params * bytes_per_param (that consumption lands in a later
    # package).
    weight_bytes: int | None = None
    # The model's own max_position_embeddings (or equivalent), when the
    # resolver found one. A context past this is not a memory question --
    # vLLM's own config validation refuses to start regardless of how much
    # GPU is free -- so the fit gate must refuse it before a launch reaches
    # the runtime, not report a `max_context_that_fits` the model itself
    # cannot serve. None when the resolver could not determine one, which
    # must never be read as "no limit": callers pass it through unclamped.
    native_window: int | None = None
    # Speculative decoding, when the operator turned it on. Trailing and
    # defaulted for the same reason as the two fields above: every existing
    # construction of this request is unchanged, and ``None`` means the launch
    # decodes one token per step, which is what every launch did before this
    # existed. The fit gate charges its weights and its extra cache when it is
    # present and changes nothing when it is not.
    speculative: SpeculativeSpec | None = None
