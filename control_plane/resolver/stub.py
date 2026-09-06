"""Day-0 stub. Returns the frozen fixture shapes and nothing else.

Agents D and E cannot start without a ``ResolverPort`` that returns real
contract types, so this exists from the first hour and is deleted at
integration. It never invents a shape: an unknown id is a clear error, not a
plausible-looking guess that silently poisons a memory calculation.
"""

from __future__ import annotations

from control_plane.contracts import ModelShape

from .support import build_verdict
from .types import ModelNotFound, ParamSource, QuantSource, Resolution

#: Architectures for the fixture models, since ``ModelShape`` carries none.
_FIXTURE_ARCHITECTURES: dict[str, tuple[str, ...]] = {
    "meta-llama/Llama-3.3-70B-Instruct": ("LlamaForCausalLM",),
    "openai/gpt-oss-120b": ("GptOssForCausalLM",),
    "Qwen/Qwen3-30B-A3B": ("Qwen3MoeForCausalLM",),
    "deepseek-ai/DeepSeek-V3": ("DeepseekV3ForCausalLM",),
}


def _fixture_shapes() -> dict[str, ModelShape]:
    from tests.fixtures import MODEL_SHAPES  # imported late; tests are not a dependency

    shapes: dict[str, ModelShape] = {}
    for short_name, shape in MODEL_SHAPES.items():
        shapes[short_name] = shape
        shapes[shape.model_id] = shape
        shapes[shape.model_id.lower()] = shape
    return shapes


class StubResolver:
    """Implements ``ResolverPort`` against the day-0 fixtures."""

    def __init__(self) -> None:
        self._shapes = _fixture_shapes()

    def resolve(self, model_id: str, dtype: str | None = None) -> ModelShape:
        shape = self._shapes.get(model_id) or self._shapes.get(model_id.lower())
        if shape is None:
            raise ModelNotFound(
                f"the day-0 stub resolver knows only {sorted(set(s.model_id for s in self._shapes.values()))}; "
                f"{model_id!r} needs the real resolver"
            )
        if dtype and dtype != shape.dtype:
            from dataclasses import replace

            from .quant_detect import normalize_override

            shape = replace(shape, dtype=normalize_override(dtype))
        return shape

    def resolve_full(self, model_id: str, dtype: str | None = None) -> Resolution:
        shape = self.resolve(model_id, dtype)
        architectures = _FIXTURE_ARCHITECTURES.get(shape.model_id, ())
        return Resolution(
            shape=shape,
            revision="fixture",
            param_source=ParamSource.CONFIG_ESTIMATE,
            quant_source=QuantSource.QUANT_CONFIG,
            support=build_verdict(architectures, shape.dtype),
            warnings=["shape came from the day-0 fixture stub, not from the hub"],
            architectures=architectures,
        )

    def resolve_gguf(self, path: str) -> ModelShape:
        raise ModelNotFound("the day-0 stub resolver does not read GGUF files")

    def supported_by(self, shape: ModelShape, runtime: str) -> tuple[bool, str]:
        architectures = _FIXTURE_ARCHITECTURES.get(shape.model_id, ())
        return build_verdict(architectures, shape.dtype).for_runtime(runtime).as_tuple()

    def available_quants(self, model_id: str) -> list[str]:
        return [self.resolve(model_id).dtype]
