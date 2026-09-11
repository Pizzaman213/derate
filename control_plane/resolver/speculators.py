"""Which speculative-decoding methods a checkpoint declares, and what they cost.

Speculative decoding is the one lever that raises decode throughput without
changing the model, the quantization or the hardware. Every other lever this
project offers moves a number the fit gate already prices; this one moves the
*rate*, and it is invisible on the model screen because nothing here used to
look for it.

Two rules, both of which a simpler version of this got wrong.

**Read what the config declares, never the architecture name.** There is more
than one mechanism and a checkpoint can carry two at once:
``tests/resolver_data/deepseek-v4-flash.config.json`` has
``num_nextn_predict_layers: 1`` *and* ``dspark_block_size: 5``. A table keyed on
``DeepseekV4ForCausalLM`` would have to guess which; the config says both, in
fields the runtime reads too.

**Never offer what cannot be budgeted.** An option whose extra parameters this
module cannot derive is reported with ``draft_params=None`` and is not
launchable. The whole product is a gate that refuses launches which will run out
of memory, and a method whose weight cost is unknown is a launch nothing has
checked. Naming it and saying so is useful; offering it is not.

The methods themselves are also checked against the runtime image, which is a
separate question and lives in ``imageprobe.py``: this module says what the
*checkpoint* supports, that one says what the *image* can load, and a method
that clears one and not the other is a launch that passes every gate here and
dies at load.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from control_plane.contracts.plan import SpeculativeMethod

from .config_map import Mapped
from .params import ParamBreakdown

#: Drafted tokens offered for ``ngram``, which reads no weights and so has no
#: checkpoint-derived count of its own. A control default and an upper bound on
#: a control -- neither is a claim about any model, and neither is measured.
NGRAM_DEFAULT_TOKENS = 5
NGRAM_MAX_TOKENS = 10

#: Drafted tokens offered for a re-run head (EAGLE, EAGLE3, Medusa).
#:
#: These do NOT get their count from their layer count, the way an MTP module
#: does. An MTP module drafts one position per nextn layer; an EAGLE head is a
#: single layer run repeatedly, so how far it drafts is a decision, not a
#: property of the checkpoint. A control default and a bound on a control --
#: neither is measured, and neither is a claim about any head.
HEAD_DEFAULT_TOKENS = 3
HEAD_MAX_TOKENS = 8

#: The methods whose draft is re-run rather than unrolled across layers.
_RERUN_METHODS = frozenset(
    {SpeculativeMethod.EAGLE, SpeculativeMethod.EAGLE3, SpeculativeMethod.MEDUSA}
)


@dataclass(frozen=True)
class SpeculativeOption:
    """One method this checkpoint can be served with, and what it costs.

    ``draft_params`` and ``draft_bytes`` are ``None`` together and mean the same
    thing: the cost was not derived, so the fit gate cannot budget it and
    :attr:`launchable` is False.
    """

    method: SpeculativeMethod
    #: Drafted tokens per step. The default is what the checkpoint declares
    #: where it declares one; the maximum is never larger, because drafting
    #: past the heads a checkpoint carries requires the runtime to loop one,
    #: which is a claim about the runtime that nothing here has verified.
    default_tokens: int
    max_tokens: int
    draft_params: int | None
    draft_bytes: int | None
    #: "checkpoint" when the config declared this mechanism, "method" when it
    #: needs no model support at all.
    source: str
    #: The config key that declared it, so a reader can go and look. Empty for
    #: a method no config declares.
    declared_by: str
    #: One sentence, shown verbatim. Says what the method does and what it
    #: costs -- and, when the cost is unknown, says that instead.
    note: str
    #: The draft's own KV cache as a fraction of the target's at the same
    #: context, from the two shapes' attention geometry. Zero for a method that
    #: loads no separate model. The fit gate charges it -- see
    #: ``fit/calculator.py::speculative_kv_bytes_per_rank``, and the launch it
    #: killed before anyone was charging it.
    draft_kv_ratio: float = 0.0

    @property
    def launchable(self) -> bool:
        """Whether the fit gate can price this. See the module docstring."""
        return self.draft_bytes is not None

    def as_dict(self) -> dict:
        return {
            "method": self.method.value,
            "default_tokens": self.default_tokens,
            "max_tokens": self.max_tokens,
            "draft_params": self.draft_params,
            "draft_bytes": self.draft_bytes,
            "draft_kv_ratio": self.draft_kv_ratio,
            "source": self.source,
            "declared_by": self.declared_by,
            "note": self.note,
            "launchable": self.launchable,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SpeculativeOption":
        return cls(
            method=SpeculativeMethod(data["method"]),
            default_tokens=int(data["default_tokens"]),
            max_tokens=int(data["max_tokens"]),
            draft_params=data.get("draft_params"),
            draft_bytes=data.get("draft_bytes"),
            draft_kv_ratio=float(data.get("draft_kv_ratio") or 0.0),
            source=data.get("source", "checkpoint"),
            declared_by=data.get("declared_by", ""),
            note=data.get("note", ""),
        )


def _mtp_bytes(
    mtp_params: int,
    total_params: int,
    weight_bytes: int | None,
    bytes_per_param: float,
) -> int:
    """Bytes the MTP module occupies, as the exact inverse of the subtraction.

    ``resolver.py`` scales a measured ``weight_bytes`` down by the MTP share the
    moment it excludes those parameters from the total::

        weight_bytes *= total_params / (total_params + mtp)

    so what it removed was ``weight_bytes_after * mtp / total_params``. Deriving
    it that way rather than from ``mtp * bytes_per_param`` matters on a
    mixed-precision repo, where the dtype formula and the shards on disk
    disagree by gigabytes -- and it makes turning speculative decoding on add
    back precisely what turning it off took away.

    Falls back to the dtype formula when nothing measured the shards.
    """
    if weight_bytes and weight_bytes > 0 and total_params > 0:
        return int(round(weight_bytes * mtp_params / total_params))
    return int(mtp_params * bytes_per_param)


def detect(
    mapped: Mapped,
    breakdown: ParamBreakdown,
    *,
    total_params: int,
    weight_bytes: int | None,
    bytes_per_param: float,
) -> tuple[SpeculativeOption, ...]:
    """Every speculative method this checkpoint can be served with.

    *total_params* and *weight_bytes* are the reconciled figures -- what a
    runtime loads by default and the measured bytes for it -- both already
    excluding the MTP module. See :func:`_mtp_bytes`.

    Never empty: ``ngram`` needs no model support, so it is offered for every
    model including the dense ones that declare nothing.
    """
    options: list[SpeculativeOption] = []

    if mapped.num_nextn_predict_layers > 0 and breakdown.mtp > 0:
        heads = mapped.num_nextn_predict_layers
        draft_bytes = _mtp_bytes(
            breakdown.mtp, total_params, weight_bytes, bytes_per_param
        )
        options.append(
            SpeculativeOption(
                method=SpeculativeMethod.MTP,
                # The declared head count is also the ceiling: one head drafts
                # one token, and drafting more means running a head repeatedly.
                default_tokens=heads,
                max_tokens=heads,
                draft_params=breakdown.mtp,
                draft_bytes=draft_bytes,
                source="checkpoint",
                declared_by="num_nextn_predict_layers",
                note=(
                    f"the checkpoint carries a {breakdown.mtp / 1e9:.1f}B-parameter "
                    f"multi-token-prediction module that no runtime loads unless "
                    f"speculative decoding is on; enabling it costs "
                    f"{draft_bytes / 1e9:.1f} GB of weights and drafts "
                    f"{heads} token{'s' if heads != 1 else ''} per step"
                ),
            )
        )

    if mapped.dspark_block_size > 0 or mapped.dspark_target_layer_ids:
        block = mapped.dspark_block_size
        layers = len(mapped.dspark_target_layer_ids)
        options.append(
            SpeculativeOption(
                method=SpeculativeMethod.DSPARK,
                default_tokens=block,
                max_tokens=block,
                # Not derived, and deliberately not estimated.
                # `dspark_target_layer_ids` names layers the model already has,
                # so this is not a decoder layer's worth of new weights the way
                # the MTP module is -- but "not that" is not a figure, and the
                # rank-256 markov head's own parameterisation is not something
                # this build has read out of a checkpoint. Detected, named, and
                # not offered.
                draft_params=None,
                draft_bytes=None,
                source="checkpoint",
                declared_by="dspark_block_size",
                note=(
                    f"the config declares DSpark over {layers} existing layer"
                    f"{'s' if layers != 1 else ''} at block size {block}, but "
                    f"derate has not derived what that module weighs, so the fit "
                    f"gate cannot budget it and this build will not launch it"
                ),
            )
        )

    options.append(
        SpeculativeOption(
            method=SpeculativeMethod.NGRAM,
            default_tokens=NGRAM_DEFAULT_TOKENS,
            max_tokens=NGRAM_MAX_TOKENS,
            # Zero, not None: this is derived and it is genuinely nothing.
            # ngram drafts by looking the prompt up in itself -- no second
            # model, no extra weights, nothing read per token.
            draft_params=0,
            draft_bytes=0,
            source="method",
            declared_by="",
            note=(
                "drafts by matching the recent output against the prompt, so it "
                "loads no weights and needs no support from the checkpoint; it "
                "helps where output repeats the input -- code edits, extraction, "
                "structured formats -- and does nothing on prose"
            ),
        )
    )

    return tuple(options)


# --------------------------------------------------------------------------
# external heads: a draft that ships in its own repository
# --------------------------------------------------------------------------
#
# Everything above reads the TARGET's config. A separately-published head is
# not in it at all -- `Qwen/Qwen3-Next-80B-A3B-Instruct` declares no
# `num_nextn_predict_layers` while the pinned image happily loads
# `Qwen3NextMTP` -- so the operator names the repository and this half prices
# what they named.

#: Head architecture -> the method name `--speculative-config` wants.
#:
#: Prefix matching rather than an exact table, and deliberately: the pinned
#: image registers 61 speculator classes and the list grows with every release
#: (`Eagle3Qwen3ForCausalLM`, `LlamaForCausalLMEagle3`, `MiMoV2MTPModel`,
#: `Qwen3DSparkModel`, `DSparkDraftModel`...). An exact table would refuse a
#: head the runtime can load the day after it ships, which is the failure mode
#: `imageprobe.py` exists to end. Whether the image can actually load the class
#: is a separate question, asked separately, against the image's own registry.
_METHOD_MARKERS: tuple[tuple[str, SpeculativeMethod], ...] = (
    # Order matters: `EagleDeepSeekMTPModel` is an EAGLE head over an MTP
    # module, and it is loaded as EAGLE.
    ("eagle3", SpeculativeMethod.EAGLE3),
    ("eagle", SpeculativeMethod.EAGLE),
    ("medusa", SpeculativeMethod.MEDUSA),
    ("dspark", SpeculativeMethod.DSPARK),
    # `DFlash2DraftModel`, `DFlashLagunaForCausalLM`,
    # `DFlashMuseGlimmerAssistantModel` -- four classes in the pinned image, one
    # method name. It was missing here until a real head was pointed at it and
    # came back "does not recognise as a draft head", while the image had been
    # able to load it all along; the same failure `imageprobe.py` exists to end,
    # in a different table.
    ("dflash", SpeculativeMethod.DFLASH),
    ("mtp", SpeculativeMethod.MTP),
)


def method_for_head(architectures: tuple[str, ...]) -> SpeculativeMethod | None:
    """Which method loads this head, from the class name it declares.

    ``None`` when nothing recognises it, which is a refusal rather than a
    default: guessing a method for an unknown class would produce a launch
    that starts and drafts with the wrong algorithm.
    """
    for arch in architectures:
        lowered = arch.lower()
        for marker, method in _METHOD_MARKERS:
            if marker in lowered:
                return method
    return None


def _kv_ratio(head_shape: Any, base_shape: Any) -> float:
    """The head's KV cache over the target's, at the same context.

    Per layer per token a transformer caches ``2 * num_kv_heads * head_dim``
    elements, so the ratio of two caches is the ratio of
    ``layers * kv_heads * head_dim`` -- and the element size cancels, which is
    what makes this answerable without knowing the KV dtype.

    A ratio rather than a byte count because the byte count depends on the
    context, and the context is not decided until the fit gate runs: a plan
    with no ``?ctx=`` derives one from what fits. Handing the gate a fraction
    lets it apply this to whatever context it settles on.

    Zero when either shape cannot be measured, which leaves the head
    unbudgeted -- so callers treat a zero as "no separate cache", which is true
    for ngram and for an in-checkpoint MTP module.
    """
    def weight(shape: Any) -> float:
        return (
            max(1, getattr(shape, "num_layers", 0) or 0)
            * max(1, getattr(shape, "num_kv_heads", 0) or 0)
            * max(1, getattr(shape, "effective_head_dim", 0) or 0)
        )

    base = weight(base_shape)
    return (weight(head_shape) / base) if base > 0 else 0.0


def target_type_conflict(declared: str, actual: str) -> bool:
    """Whether a head's declared target type rules THIS target out.

    False for everything uncertain, and that is the whole design: a head that
    declares nothing (most of them, including good ones) and a head whose
    declaration is merely coarser than the target's must both pass.

    **This is the only signal that separates a vision head from a text one.**
    The geometry cannot -- ``AngelSlim/Qwen3-VL-30B-A3B-Instruct_eagle3``,
    ``nvidia/Qwen3-30B-A3B-Thinking-2507-Eagle3`` and ``Qwen/Qwen3-30B-A3B``
    report the same hidden size (2048), the same vocabulary (151936) and the
    same ``vision_params`` (0). The VL head declares
    ``target_model_type: "qwen3_vl"`` and the text head declares nothing.

    Matching is on SEGMENT boundaries, not raw prefixes, so granularity is not
    a conflict while a sibling family is::

        qwen3      vs qwen3_moe -> no conflict (coarser, and consistent)
        qwen3_moe  vs qwen3_moe -> no conflict
        qwen3_vl   vs qwen3_moe -> CONFLICT   (siblings, neither contains other)
        llama      vs qwen3_moe -> CONFLICT

    A bare `startswith` would have accepted the VL head, since `qwen3_vl` and
    `qwen3_moe` share the `qwen3` stem that a raw prefix test never sees the end
    of.
    """
    left = (declared or "").strip().lower()
    right = (actual or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return False
    return not (right.startswith(left + "_") or left.startswith(right + "_"))


def head_option(
    head: Any,
    base_shape: Any,
    *,
    image_speculators: frozenset[str] | None = None,
    target_model_type: str = "",
) -> SpeculativeOption:
    """Price one externally-published head against the model it would draft for.

    *head* is a full ``Resolution`` of the head's own repository -- it is a
    repo like any other, which is what makes this tractable at all: the same
    mapper that sizes a model sizes a head, and the same weight index measures
    it.

    Four things have to hold, and each failure comes back as a non-launchable
    option carrying the sentence for why rather than as an exception. The
    screen shows a head that cannot be used and says what is wrong with it,
    which is more useful than a head that silently is not offered.
    """
    archs = tuple(getattr(head, "architectures", ()) or ())
    method = method_for_head(archs)
    name = archs[0] if archs else "?"

    def refuse(note: str, guess: SpeculativeMethod = SpeculativeMethod.EAGLE3):
        return SpeculativeOption(
            method=method or guess,
            default_tokens=1,
            max_tokens=1,
            draft_params=None,
            draft_bytes=None,
            source="head",
            declared_by=name,
            note=note,
        )

    if method is None:
        return refuse(
            f"{head.shape.model_id} declares {name}, which this build does not "
            f"recognise as a draft head; nothing here knows which speculative "
            f"algorithm would load it"
        )

    # The image's own registry, when it could be asked. Absent, no refusal --
    # the same contract as every other probe result in this project.
    if image_speculators is not None and name not in image_speculators:
        return refuse(
            f"the runtime image does not register {name}, so this head would "
            f"clear every gate here and fail at load; pull a newer image or "
            f"pick a head the image knows",
            method,
        )

    # Compatibility, mechanically. A head is trained against one target's
    # residual stream and one tokenizer; both are visible as numbers, and a
    # mismatch is a launch that dies at load rather than a quality question.
    if head.shape.hidden_size != base_shape.hidden_size:
        return refuse(
            f"{name} was trained for a hidden size of {head.shape.hidden_size} "
            f"and {base_shape.model_id} has {base_shape.hidden_size}; a head "
            f"reads the target's residual stream directly, so these must match",
            method,
        )
    if head.shape.vocab_size != base_shape.vocab_size:
        return refuse(
            f"{name} expects a {head.shape.vocab_size}-token vocabulary and "
            f"{base_shape.model_id} has {base_shape.vocab_size}; the head would "
            f"draft tokens the target cannot verify",
            method,
        )

    # What the head says it was trained against, when it says. This catches
    # what no dimension can: a vision head for a text model of the same width.
    # An optional kwarg that degrades, like the live-memory one and like
    # `image_speculators` above -- absence is never a refusal.
    declared = str(getattr(head, "target_model_type", "") or "")
    if target_model_type and target_type_conflict(declared, target_model_type):
        return refuse(
            f"{head.shape.model_id} declares it was trained for a "
            f"{declared!r} target and {base_shape.model_id} is "
            f"{target_model_type!r}; the dimensions match but the residual "
            f"stream this head reads is a different model's",
            method,
        )

    # Measured, or not offered. The analytic split for a head is a floor: the
    # projection that folds the target's hidden states together is not modeled,
    # and on the one head here whose shards can be counted the estimate lands
    # 16 percent low. Under-charging is the wrong direction for a memory gate,
    # so a head whose repository carries no weight index is named and refused
    # rather than budgeted from a formula.
    measured = getattr(head, "weight_bytes", None)
    if not measured or measured <= 0:
        return refuse(
            f"{head.shape.model_id} publishes no weight index, so its size could "
            f"only be estimated from its config -- and that estimate is a floor "
            f"for a draft head. derate does not budget what it cannot measure",
            method,
        )

    # An MTP or DSpark head unrolls one drafted position per layer; an EAGLE or
    # Medusa head is one layer re-run, so its count is an operator choice.
    if method in _RERUN_METHODS:
        tokens, ceiling = HEAD_DEFAULT_TOKENS, HEAD_MAX_TOKENS
    else:
        tokens = ceiling = max(1, head.shape.num_layers)
    kv_ratio = _kv_ratio(head.shape, base_shape)
    return SpeculativeOption(
        method=method,
        default_tokens=tokens,
        max_tokens=ceiling,
        draft_params=int(head.shape.total_params),
        draft_bytes=int(measured),
        draft_kv_ratio=kv_ratio,
        source="head",
        declared_by=name,
        note=(
            f"{head.shape.model_id}: a {name} head of "
            f"{head.shape.total_params / 1e9:.2f}B parameters, measured at "
            f"{measured / 1e9:.2f} GB on disk, trained to draft for a "
            f"{base_shape.hidden_size}-wide target — which is what "
            f"{base_shape.model_id} is"
        ),
    )
