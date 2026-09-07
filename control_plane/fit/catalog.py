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


#: The four frozen shapes, in the order the picker shows them.
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
