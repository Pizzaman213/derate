"""Day-0 stub. Returns the frozen fixture shapes and nothing else.

The real resolver landed and `node.py` wires it in production behind
`GatewayDeps(strict=True, ...)`, which refuses to start if any port -- this
one included -- is still missing. This file stayed anyway: `__init__.py`
exports `StubResolver` as this package's public fake, and
`tests/unit/test_resolver.py`, `test_setup.py`, `test_gateway_models_api.py`,
`test_gateway_runtime.py`, `test_gateway_restart.py` and `test_gateway.py`
reach for it wherever a test needs a `ResolverPort` without the real
HuggingFace lookup. It never invents a shape: an unknown id is a clear error,
not a plausible-looking guess that silently poisons a memory calculation.
"""

from __future__ import annotations

from control_plane.contracts import ModelShape, SpeculativeMethod

from .speculators import (
    NGRAM_DEFAULT_TOKENS,
    NGRAM_MAX_TOKENS,
    SpeculativeOption,
)
from .support import build_verdict
from .types import ModelNotFound, ParamSource, QuantSource, Resolution

#: The one speculative option a stub with no config.json can honestly offer.
#: Built from `speculators.py`'s own constants rather than retyped, so the two
#: cannot drift into disagreeing about how many tokens ngram drafts.
_NGRAM_OPTION = SpeculativeOption(
    method=SpeculativeMethod.NGRAM,
    default_tokens=NGRAM_DEFAULT_TOKENS,
    max_tokens=NGRAM_MAX_TOKENS,
    draft_params=0,
    draft_bytes=0,
    source="method",
    declared_by="",
    note=(
        "drafts by matching the recent output against the prompt, so it loads "
        "no weights and needs no support from the checkpoint"
    ),
)

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
            # ngram and nothing else, and it is not a placeholder. This stub
            # has no config.json to read, so it cannot know whether a fixture
            # shape declares an MTP module -- but ngram needs no support from
            # any checkpoint, so offering it is the one speculative claim that
            # is true without reading anything. An empty tuple here would be a
            # different and false claim: that this model supports none.
            speculators=(_NGRAM_OPTION,),
        )

    def resolve_gguf(self, path: str) -> ModelShape:
        raise ModelNotFound("the day-0 stub resolver does not read GGUF files")

    def supported_by(self, shape: ModelShape, runtime: str) -> tuple[bool, str]:
        architectures = _FIXTURE_ARCHITECTURES.get(shape.model_id, ())
        return build_verdict(architectures, shape.dtype).for_runtime(runtime).as_tuple()

    def available_quants(self, model_id: str) -> list[str]:
        return [self.resolve(model_id).dtype]
