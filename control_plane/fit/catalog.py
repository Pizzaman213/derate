"""The curated model list, server side.

This lived only in ``ui/src/api/catalog.ts``, which was fine while the picker
was its only consumer. The capacity answer needs the same list -- and the
answer has to come from the fit gate, not the browser -- so the list moves
here and the UI fetches it. Duplicating it is cheaper today and guarantees the
two drift.

Not a registry of what is installed: it is a shortlist of shapes worth asking
about, and every one of them is resolved for real before any verdict is taken.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CuratedModel:
    model_id: str
    label: str
    detail: str
    default_context: int
    default_concurrency: int


#: The shortlist, in the order the picker shows them.
#:
#: Eight, not four. `MAX_CATALOG_MODELS` has always allowed eight and the list
#: had half that, so the cap was inert and the strip was four research shapes
#: that mostly do not fit on one machine -- a shortlist whose every entry is
#: refused teaches nothing about the hardware.
#:
#: Every `detail` here is measured, not copied off a model card: the figures
#: below came out of `ModelResolver.resolve_full` on 2026-09-11.
#: `tests/unit/test_catalog.py` holds the list to its own cap and its own
#: shape, and `tests/model_sweep.py --curated` resolves every id for real --
#: a withdrawn or renamed repository is a dead row on the busiest screen in
#: the product, and nothing used to notice.
CURATED_MODELS: tuple[CuratedModel, ...] = (
    CuratedModel(
        model_id="openai/gpt-oss-120b",
        label="gpt-oss-120b",
        detail="MoE · mxfp4 · sliding window · 116.8B",
        default_context=32768,
        default_concurrency=16,
    ),
    CuratedModel(
        model_id="Qwen/Qwen3-30B-A3B",
        label="qwen3-30b-a3b",
        detail="MoE · bf16 · 30.5B, 3.3B active",
        default_context=32768,
        default_concurrency=8,
    ),
    CuratedModel(
        model_id="meta-llama/Llama-3.3-70B-Instruct",
        label="llama-3.3-70b",
        detail="dense · GQA 8:1 · bf16 · 70.6B",
        default_context=131072,
        default_concurrency=32,
    ),
    CuratedModel(
        model_id="deepseek-ai/DeepSeek-V3",
        label="deepseek-v3",
        detail="MoE · MLA · fp8 · 671.0B",
        default_context=32768,
        default_concurrency=16,
    ),
    # The four below were added on 2026-09-11. The list had nothing that
    # fits on one Spark, nothing with a vision tower, and nothing that is
    # not a text generator -- so the strip could not show what this hardware
    # is actually good at, only what it refuses.
    CuratedModel(
        model_id="Qwen/Qwen3-8B",
        label="qwen3-8b",
        detail="dense · GQA 4:1 · bf16 · 8.2B",
        default_context=40960,
        default_concurrency=32,
    ),
    CuratedModel(
        model_id="Qwen/Qwen3-Next-80B-A3B-Instruct",
        label="qwen3-next-80b-a3b",
        detail="MoE · 512 experts · bf16 · 81.3B, 5.2B active",
        default_context=32768,
        default_concurrency=16,
    ),
    CuratedModel(
        model_id="Qwen/Qwen2.5-VL-7B-Instruct",
        label="qwen2.5-vl-7b",
        detail="vision · dense · bf16 · 8.3B, 0.6B replicated tower",
        default_context=32768,
        default_concurrency=16,
    ),
    CuratedModel(
        model_id="Qwen/Qwen3-Embedding-0.6B",
        label="qwen3-embedding-0.6b",
        detail="embedding · dense · bf16 · 0.6B",
        default_context=32768,
        default_concurrency=32,
    ),
)


def catalog_payload() -> list[dict]:
    return [
        {
            "model_id": m.model_id,
            "label": m.label,
            "detail": m.detail,
            "default_context": m.default_context,
            "default_concurrency": m.default_concurrency,
        }
        for m in CURATED_MODELS
    ]
