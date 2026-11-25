"""Which KV cache width a plan actually stores, and which ones it may offer.

The single home of the Serve panel's "Memory -- kv" list. What decides a row,
none of which the fit gate knows on its own:

* The model's own width is not ``shape.dtype`` (the WEIGHT scheme). It is the
  width the engine picks when told nothing (``_UNTOLD``): the checkpoint's
  compute dtype -- or, on vLLM, a KV scheme its quantization config declares
  (`Resolution.checkpoint_kv_dtype`). A declared width is PRICED by name, so
  the gate sizes it and the silicon check judges it, and is NOT passed: vLLM
  resolves the same width itself, and passing it would make the record read
  as the operator's choice and refuse a custom command they never paired it
  with.
* derate's vocabulary has always used ``fp16`` / ``bf16`` / ``half`` for "the
  model's own" (`settings.default_kv_dtype`, autoadopt), so those stay own.
  ``float16`` / ``bfloat16`` -- the names the picker shows -- mean that width.
  The one equal to the compute dtype is the model's own, passed by name only
  to override a declared scheme. The other is refused: measured on the GB10
  here, `reshape_and_cache_flash` writes a bfloat16 key into a float16 cache
  by copying its bits, so it would corrupt the cache.
* Every other reason a width is out -- not priced, not renderable, wrong
  attention geometry, a runtime that cannot be told, a machine with no kernel
  -- is a sentence, so the screen can say which one it was.
"""

from __future__ import annotations

import dataclasses
from typing import Callable

from control_plane.contracts.quant import normalize_dtype
from control_plane.engines import spec_for
from control_plane.fit.kv import (
    MODEL_OWN_KV_DTYPES,
    is_known_kv_dtype,
    kv_bytes_per_token,
    kv_dtype_refusal,
)
from control_plane.resolver.quant_detect import TORCH_NAMES

# `deploy.flags` is imported inside each function that needs it, as every
# gateway module does: `deploy/__init__` pulls in the manager and the sparkrun
# adapter (`engines/__init__.py` says why that door stays shut).

#: `internal_api._silicon_per_node` bound to one plan's runtime, machines,
#: weight dtype and loader: called with `kv_cache_dtype=` and `kv_label=`, it
#: answers `(refusals by node id, warnings by node id)`.
SiliconCheck = Callable[..., "tuple[dict[str, str], dict]"]

#: How each engine picks its cache width when told nothing. A table here
#: rather than an `EngineSpec` field because that dataclass is pinned field
#: for field to a copy in `deploy/flags.py` (`test_engine_registry.py`);
#: `test_kv_widths.py` holds this one to `engines.SUPPORTED` instead.
_UNTOLD: dict[str, str] = {
    # A KV scheme the checkpoint declares (`resolve_kv_cache_dtype_string`),
    # else the compute dtype.
    "vllm": "declared",
    "sglang": "compute",
    # `runtimes/tts.py` loads in bfloat16 on CUDA whatever the config says.
    "tts": "bfloat16",
    # llama.cpp's default `--cache-type-k/v` is f16 whatever the model is.
    "llamacpp": "float16",
}

_GAUDI = "fp8_inc"


@dataclasses.dataclass(frozen=True)
class OwnWidth:
    """What a launch told nothing stores this checkpoint's cache at."""

    #: `bfloat16`, `float16` or a declared `fp8_e4m3`; "" when the checkpoint
    #: does not say what it computes in.
    name: str
    #: The checkpoint's quantization config declared it (vLLM only).
    declared: bool
    #: The 16-bit width the engine computes in; "" when unknown.
    compute: str
    #: vLLM's loader for the checkpoint (`Resolution.quant_method`), "" if none.
    quant: str = ""
    #: The runtime stores one width whatever the model computes in.
    fixed: bool = False
    #: The model caches a multi-head latent, not per-head K and V.
    mla: bool = False


@dataclasses.dataclass(frozen=True)
class KvChoices:
    """The picker's answers for one plan."""

    own: OwnWidth = OwnWidth("", False, "")
    #: The model's own cache over its 16-bit rate; None when nothing is cached.
    own_ratio: float | None = None
    #: Every width this plan accepts, over the 16-bit rate.
    ratios: dict[str, float] = dataclasses.field(default_factory=dict)
    #: The flag the launch passes for each accepted width that passes one.
    flags: dict[str, str] = dataclasses.field(default_factory=dict)
    #: Every width it refuses, with the sentence that says why.
    refused: dict[str, str] = dataclasses.field(default_factory=dict)

    def wire(self) -> dict:
        return {
            "kv_dtype_ratios": self.ratios,
            "kv_dtype_own": {
                "name": self.own.name or None,
                "declared": self.own.declared,
                "ratio": self.own_ratio,
            },
            "kv_dtype_flags": self.flags,
            "kv_dtype_refused": self.refused,
        }


def _sixteen(name: str | None) -> str:
    """The torch name of a 16-bit width spelled *name*, or ""."""
    found = TORCH_NAMES.get(normalize_dtype(name) or "", "")
    return found if found in ("bfloat16", "float16") else ""


def _is_own(name: str | None) -> bool:
    return (name or "").strip().lower() in MODEL_OWN_KV_DTYPES


def own_width(resolution, runtime: str, shape=None) -> OwnWidth:
    """The width *runtime* stores this checkpoint's cache at when told nothing.

    The compute dtype follows vLLM's `_resolve_auto_dtype` (SGLang copies it):
    float16 and bfloat16 stay, float32 is downcast -- to float16 for an
    embedding model, bfloat16 otherwise.
    """
    rule = _UNTOLD.get(runtime, "compute")
    stated = str(getattr(resolution, "compute_dtype", "") or "")
    if _sixteen(rule):
        compute = rule
    elif stated == "float32":
        embedding = getattr(resolution, "modality", "") == "embedding"
        compute = "float16" if embedding else "bfloat16"
    else:
        compute = _sixteen(stated)
    declared = str(getattr(resolution, "checkpoint_kv_dtype", "") or "")
    quant = str(getattr(resolution, "quant_method", "") or "") or _forced_loader(shape)
    # compressed-tensors' scheme is applied by `Attention.__init__` only;
    # `MLAAttention` never reads it, so an MLA cache stays at the compute width.
    mla = bool(getattr(shape, "mla_latent_dim", 0))
    if quant == "compressed-tensors" and mla:
        declared = ""
    if rule == "declared" and declared:
        return OwnWidth(declared, True, compute, quant, mla=mla)
    return OwnWidth(compute, False, compute, quant, fixed=bool(_sixteen(rule)), mla=mla)


def _forced_loader(shape) -> str:
    """The `--quantization` a forced weight scheme renders as (`shape.dtype`
    is the forced one), or "". vLLM then gives attention its KV method too."""
    from control_plane.deploy.flags import render_quantization

    try:
        return render_quantization(getattr(shape, "dtype", None)) or ""
    except ValueError:
        return ""


def _sixteen_refusal(model_id: str, own: OwnWidth, name: str) -> str | None:
    """Why the 16-bit width *name* is not this model's own, or None."""
    want = _sixteen(name)
    if not want or want == own.compute:
        return None
    if own.fixed:
        return (
            f"this runtime keeps its KV cache at {own.compute} whatever "
            f"{model_id} computes in, and cannot be told another width; keep "
            f"the model's own"
        )
    if not own.compute:
        return (
            f"{model_id}'s config does not say what it computes in, so derate "
            f"cannot tell whether a {want} cache is its own width -- and the "
            f"engine copies 16-bit values into the cache unconverted, so the "
            f"wrong one corrupts it; keep the model's own"
        )
    return (
        f"{model_id} computes in {own.compute}, and the engine copies "
        f"{own.compute} values into a {want} cache bit for bit rather than "
        f"converting them, which corrupts it; choose {own.compute}"
    )


def _declared_refusal(model_id: str, own: OwnWidth, refusal: str) -> str:
    """*refusal* of the declared width, said as the checkpoint's choice."""
    instead = own.compute or "another width below"
    return (
        f"{model_id}'s quantization config declares its KV cache as {own.name}, "
        f"which vLLM applies when told nothing, and {refusal}; choose {instead} "
        f"to override it"
    )


def settle(shape, resolution, runtime: str, requested: str | None) -> tuple[str, str | None]:
    """The width a plan prices, and why it is refused.

    *requested* is the caller's `kv_dtype`; None or an own spelling means the
    model's own. The answer is what the gate is handed: "auto" for a 16-bit
    own width, the declared name for a declared one, and a 16-bit name only
    where it overrides a declared scheme.
    """
    own = own_width(resolution, runtime, shape)
    requested = requested or ""
    if _settled(own, requested) == "auto":
        return "auto", None
    if kv_bytes_per_token(shape, "bf16") <= 0 and not _is_own(requested):
        return requested, _no_cache(shape.model_id)
    if _is_own(requested):
        refusal = kv_dtype_refusal(shape, own.name)
        return own.name, (_declared_refusal(shape.model_id, own, refusal) if refusal else None)
    refusal = (
        _sixteen_refusal(shape.model_id, own, requested)
        or _e5m2_refusal(shape.model_id, own, runtime, requested)
        or kv_dtype_refusal(shape, requested)
    )
    if refusal is None:
        from control_plane.deploy.flags import kv_cache_dtype_refusal

        flag = passed(requested, own)
        refusal = kv_cache_dtype_refusal(runtime, flag) if flag else None
    return requested, refusal


#: vLLM's `Attention._init_kv_cache_quant` raises "fp8_e5m2 kv-cache is not
#: supported with fp8 checkpoints" for any loader that gives attention a KV
#: quant method. compressed-tensors and ModelOpt's mixed loader do that only
#: when the checkpoint declares a KV scheme (pinned image, attention.py:199-214).
_E5M2_REFUSING = ("fp8", "modelopt", "compressed-tensors", "quark")
_E5M2_ONLY_IF_DECLARED = ("compressed-tensors", "modelopt_mixed")


def _e5m2_refusal(model_id: str, own: OwnWidth, runtime: str, name: str) -> str | None:
    if runtime != "vllm" or (name or "").strip().lower() != "fp8_e5m2":
        return None
    if own.mla:
        return "no MLA attention backend in this image stores fp8_e5m2; choose fp8"
    if not own.quant.startswith(_E5M2_REFUSING):
        return None
    if own.quant in _E5M2_ONLY_IF_DECLARED and not own.declared:
        return None
    return (
        f"{model_id} is a {own.quant} checkpoint, and vLLM refuses fp8_e5m2 "
        f"for one at load; choose fp8_e4m3"
    )


def passed(kv_dtype: str | None, own: OwnWidth) -> str | None:
    """The `--kv-cache-dtype` value a launch passes for the settled width, or
    None. A declared own width is priced and never passed: vLLM reads it off
    the checkpoint itself."""
    from control_plane.deploy.flags import render_kv_cache_dtype

    if own.declared and (kv_dtype or "").strip().lower() == own.name:
        return None
    return render_kv_cache_dtype(kv_dtype)


def launch_kv_dtype(kv_dtype: str, choices: KvChoices) -> str | None:
    """The `kv_dtype` a launch of this plan is handed: the settled width when
    it passes a flag, else None. `/api/plan/command` and `create_deployment`
    both read this, so the preview and the launch cannot disagree."""
    return kv_dtype if passed(kv_dtype, choices.own) is not None else None


def label(kv_dtype: str, resolution, runtime: str) -> str:
    """How a refusal names *kv_dtype*: as the checkpoint's own when it is."""
    own = own_width(resolution, runtime)
    if own.declared and kv_dtype == own.name:
        return f"{kv_dtype}, which this checkpoint declares; choose {own.compute or 'another width'} to override it"
    return kv_dtype


#: Every engine's spelling of a choice the gate prices the cache by: the width
#: (vLLM's and SGLang's, llama.cpp's per-side pair), the layers kept wide, and
#: the compute dtype that `auto` is.
_KV_WIDTH_FLAGS = frozenset({
    "--kv-cache-dtype", "--kv-cache-dtype-skip-layers", "--dtype",
    "--quantization", "-q", "-ctk", "-ctv", "--cache-type-k", "--cache-type-v",
})


def refuse_kv_arg(field: str, tokens: tuple[str, ...]) -> None:
    """A KV width in `extra_args` is one the gate never priced -- on a declared
    checkpoint, a 16-bit cache in an fp8 budget; on llama.cpp, an f32 cache in
    an f16 one. Matched as the engines parse it: before any `=`, and with
    vLLM's `_` -> `-` (its parser accepts `--kv_cache_dtype`)."""
    for token in tokens:
        flag = token.split("=", 1)[0]
        if flag.startswith("--"):
            flag = flag.replace("_", "-")
        # argparse takes an unambiguous prefix (`--dtyp`), so refuse those too.
        long = len(flag) > 3 and flag.startswith("--")
        if not flag.startswith("--") and flag.startswith("-q"):
            flag = "-q"  # argparse takes `-qfp8`
        named = flag if flag in _KV_WIDTH_FLAGS else next(
            (f for f in sorted(_KV_WIDTH_FLAGS) if long and f.startswith(flag)), None)
        if named is not None:
            what = "the weight scheme as dtype" if named in ("--quantization", "-q") else "the KV width as kv_dtype"
            fix = f"set {what}" + (" and drop the custom command" if field == "custom_command" else "")
            raise ValueError(
                f"{field}: {flag} is not taken as free text -- the KV width "
                f"and what it is priced at are fit-gate inputs, so {fix}, "
                f"where the gate sizes the cache and the launch passes it"
            )


def _why_not(shape, runtime: str, own: OwnWidth, name: str, judge: SiliconCheck | None) -> str | None:
    from control_plane.deploy.flags import kv_cache_dtype_refusal, render_kv_cache_dtype

    if name == _GAUDI:
        return f"{_GAUDI} is Intel Gaudi's kernel and loads on no GPU derate places on"
    if not is_known_kv_dtype(name):
        return f"derate does not price {name}, so the gate cannot size a cache for it"
    try:
        render_kv_cache_dtype(name)
    except ValueError as exc:
        return str(exc)
    refusal = (
        _sixteen_refusal(shape.model_id, own, name)
        or _e5m2_refusal(shape.model_id, own, runtime, name)
        or kv_dtype_refusal(shape, name)
    )
    settled = _settled(own, name)
    # What the launch passes is what a runtime must be able to be told; what
    # the engine stores (a declared width it reads itself included) is what a
    # machine needs a kernel for.
    flag = passed(settled, own)
    if refusal is None and flag is not None:
        refusal = kv_cache_dtype_refusal(runtime, flag)
    stored = render_kv_cache_dtype(settled)
    if refusal is None and judge is not None and stored is not None:
        by_node, _ = judge(kv_cache_dtype=stored, kv_label=name)
        refusal = next(iter(by_node.values()), None)  # the first machine's sentence
    return refusal


def _settled(own: OwnWidth, name: str) -> str:
    """*name* as the gate is handed it: "auto" for a 16-bit own width (an own
    spelling, or the compute dtype by name with no scheme declared), the
    declared width for an own spelling on a declared checkpoint, else *name*."""
    if _is_own(name):
        return own.name if own.declared else "auto"
    if not own.declared and _sixteen(name) and _sixteen(name) == own.compute:
        return "auto"
    return name


def choices(shape, resolution, runtime: str, silicon: SiliconCheck | None) -> KvChoices:
    """Every `--kv-cache-dtype` this plan could launch with, each as its cache
    bytes per token over the 16-bit rate and the flag it passes, and every one
    it could not, each with its reason. *silicon* is None when the plan has no
    machines. It is asked only on vllm and never of MLA: the image's selector
    was asked about ordinary attention only, and no evidence is no refusal.
    """
    from control_plane.deploy.flags import VLLM_KV_CACHE_DTYPES

    own = own_width(resolution, runtime, shape)
    judge = silicon if runtime == "vllm" and not getattr(shape, "mla_latent_dim", 0) else None
    names = sorted(VLLM_KV_CACHE_DTYPES - {"auto"})
    base = kv_bytes_per_token(shape, "bf16")
    if base <= 0:
        return KvChoices(own=own, refused={name: _no_cache(shape.model_id) for name in names})
    template = spec_for(runtime).kv_cache_dtype_arg or ""
    out = KvChoices(own=own, own_ratio=kv_bytes_per_token(
        shape, own.name if own.declared else "auto") / base)
    for name in names:
        reason = _why_not(shape, runtime, own, name, judge)
        if reason is not None:
            out.refused[name] = reason
            continue
        out.ratios[name] = kv_bytes_per_token(shape, name) / base
        flag = passed(_settled(own, name), own)
        if flag is not None and template:
            out.flags[name] = template.format(kv_cache_dtype=flag)
    return out


def _no_cache(model_id: str) -> str:
    return (
        f"{model_id} keeps no per-token cache -- every layer holds a fixed "
        f"state -- so there is no KV width to choose"
    )
