"""Resolver-owned types. The contract type ``ModelShape`` carries no room for
provenance, warnings or a support verdict, so those ride alongside it here.

``resolve()`` satisfies ``ResolverPort`` and returns the bare ``ModelShape``.
``resolve_full()`` returns the ``Resolution``, which is what the UI and the
deployment manager should show a human before anything is launched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from control_plane.contracts import ModelShape


class ResolverError(RuntimeError):
    """Base for every failure this component raises."""


class ModelNotFound(ResolverError):
    """No such model id, or the repo has no config we can read."""


class MetadataUnavailable(ResolverError):
    """The repo exists but the metadata we need could not be fetched."""


class UnsupportedArchitecture(ResolverError):
    """The config is readable but not a shape we can describe honestly."""


class ParamSource(str, Enum):
    """Where ``total_params`` came from, best first."""

    SAFETENSORS_HEADERS = "safetensors_headers"  # exact, per-tensor
    HUB_SAFETENSORS_INDEX = "hub_safetensors_index"  # hub's per-dtype tally
    INDEX_TOTAL_SIZE = "index_total_size"  # shard bytes / bytes per element
    GGUF_TENSORS = "gguf_tensors"  # exact, per-tensor
    CONFIG_ESTIMATE = "config_estimate"  # analytic, last resort


class QuantSource(str, Enum):
    """Where ``dtype`` came from, best first."""

    QUANT_CONFIG = "quantization_config"
    HF_QUANT_CONFIG = "hf_quant_config.json"
    GGUF_FILE_TYPE = "gguf_file_type"
    TORCH_DTYPE = "torch_dtype"
    REPO_NAME = "repo_name"
    OVERRIDE = "caller_override"
    DEFAULTED = "defaulted"  # nothing said; bf16 plus a warning


class SupportLevel(str, Enum):
    SUPPORTED = "supported"  # verified to load on this runtime
    UNVERIFIED = "unverified"  # plausible, not on our known-good list
    UNSUPPORTED = "unsupported"  # known not to load


@dataclass(frozen=True)
class RuntimeSupport:
    runtime: str  # "vllm" | "sglang"
    level: SupportLevel
    reason: str
    #: The runtime image's own version string, when a probe has run --
    #: ``ImageProbe.version``. ``None`` on a machine with no docker or no
    #: image pulled, which is the same "older answer, not a wrong one"
    #: fallback the rest of this module already makes for the probe itself.
    version: str | None = None

    @property
    def ok(self) -> bool:
        return self.level is SupportLevel.SUPPORTED

    def as_tuple(self) -> tuple[bool, str]:
        return self.ok, self.reason


@dataclass(frozen=True)
class QuantVariant:
    """One obtainable set of weights for a model.

    The unit a person actually picks. ``repo_id`` is what would be launched --
    a quantization is a different repository, not a flag, because neither serve
    command template carries ``--quantization`` and the only model identifier
    they interpolate is the repo id.

    ``label`` is kept beside ``dtype`` on purpose. ``dtype`` is the canonical
    key this codebase prices with; ``label`` is what the publisher called it,
    and rendering "q4_k_m" for a file named "UD-Q4_K_XL" would be a paraphrase
    of a string somebody else chose.

    ``file_bytes`` is a measurement or it is ``None``. It is never a table
    estimate -- for a GGUF variant the hub reports the real size, and an
    Unsloth Dynamic mix has no fixed bits-per-weight for a table to hold.
    """

    dtype: str
    label: str
    repo_id: str
    source: str  # "self" | "repo_name" | "tags" | "gguf_file"
    gguf_file: str | None = None
    file_bytes: int | None = None
    downloads: int | None = None
    launchable: bool = True
    note: str = ""
    #: Files in this quantization. Greater than one when llama.cpp split the
    #: build; ``file_bytes`` is their sum, and ``gguf_file`` names only the
    #: first, so without this the size and the filename disagree about how much
    #: is being described.
    shard_count: int = 1
    #: Every file in the set, in shard order. ``gguf_file`` is the first of
    #: these. Carried so a screen can show what a summed size is the sum *of*
    #: rather than asserting a count nobody can check.
    shard_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuantRequirement:
    """What silicon a quantization scheme needs."""

    dtype: str
    native_compute_capability: float | None
    emulated_below_native: bool
    note: str

    def check(self, compute_capability: str) -> tuple[bool, str]:
        """Can a device at this compute capability run this scheme?"""
        need = self.native_compute_capability
        if need is None:
            return True, f"{self.dtype} needs no special compute capability"
        try:
            have = float(compute_capability)
        except (TypeError, ValueError):
            return False, (
                f"compute capability {compute_capability!r} is unreadable; "
                f"{self.dtype} needs sm_{need}"
            )
        if have >= need:
            return True, f"{self.dtype} is native at sm_{compute_capability}"
        if self.emulated_below_native:
            return True, (
                f"{self.dtype} runs emulated at sm_{compute_capability}; "
                f"native from sm_{need}. {self.note}".strip()
            )
        return False, (
            f"{self.dtype} needs sm_{need}, node reports sm_{compute_capability}. "
            f"{self.note}".strip()
        )


@dataclass(frozen=True)
class SupportVerdict:
    """Whether the runtimes can actually load this, quantization included."""

    architectures: tuple[str, ...]
    runtimes: tuple[RuntimeSupport, ...]
    quant: QuantRequirement

    def for_runtime(self, runtime: str) -> RuntimeSupport:
        want = runtime.strip().lower()
        for entry in self.runtimes:
            if entry.runtime == want:
                return entry
        return RuntimeSupport(
            runtime=want,
            level=SupportLevel.UNVERIFIED,
            reason=f"no support data for runtime {runtime!r}",
        )

    @property
    def any_runtime_ok(self) -> bool:
        return any(entry.ok for entry in self.runtimes)


@dataclass
class Resolution:
    """A ``ModelShape`` plus everything a human needs to trust it."""

    shape: ModelShape
    revision: str  # commit sha when known, else the requested ref
    param_source: ParamSource
    quant_source: QuantSource
    support: SupportVerdict
    warnings: list[str] = field(default_factory=list)
    #: Real bytes the weights occupy on disk, when a source could tell us.
    #: Mixed-precision repos (MXFP4 experts with bf16 attention) cost more than
    #: ``total_params * bytes_per_param``; prefer this when it is not None.
    weight_bytes: int | None = None
    architectures: tuple[str, ...] = ()
    model_type: str = ""
    max_position_embeddings: int | None = None
    #: Analytic parameter split, for whoever wants to see the arithmetic.
    param_breakdown: dict[str, int] = field(default_factory=dict)
    resolved_at: float = 0.0
    from_cache: bool = False
    elapsed_ms: float = 0.0

    @property
    def model_id(self) -> str:
        return self.shape.model_id

    @property
    def modality(self) -> str:
        """Which endpoint family this model answers on: text, embedding,
        speech or transcription.

        Derived rather than stored, so nothing has to be re-cached when a new
        architecture joins the audio table -- and so a resolution written by
        an older build reports the current answer rather than a stale one.
        The import is function-local because ``support`` imports this module.
        """
        from .support import modality_for

        return modality_for(self.architectures)

    def effective_weight_bytes(self) -> int:
        """Bytes to charge for weights. Never smaller than the dtype figure."""
        dtype_bytes = int(self.shape.total_params * self.shape.bytes_per_param())
        if self.weight_bytes is None:
            return dtype_bytes
        return max(dtype_bytes, self.weight_bytes)
