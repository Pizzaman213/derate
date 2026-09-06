"""Can the runtime actually load this?

Weights fitting and the runtime supporting the architecture are different
questions, and the second one is the cheaper to answer. A model that clears the
byte check and fails here should be refused before launch rather than at load
time, five minutes in.
"""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.contracts import ModelShape, NodeProfile
from control_plane.contracts.quant import quant_info

from .types import QuantRequirement, RuntimeSupport, SupportLevel, SupportVerdict

#: Architectures each runtime is known to load. Not exhaustive -- both projects
#: add models weekly -- so an absent name is reported as unverified, not as a
#: refusal. A name that is present and known broken is reported as unsupported.
VLLM_ARCHITECTURES: frozenset[str] = frozenset(
    {
        "AquilaForCausalLM", "ArcticForCausalLM", "BaiChuanForCausalLM", "BaichuanForCausalLM",
        "BloomForCausalLM", "ChatGLMModel", "CohereForCausalLM", "Cohere2ForCausalLM",
        "DbrxForCausalLM", "DeciLMForCausalLM", "DeepseekForCausalLM", "DeepseekV2ForCausalLM",
        "DeepseekV3ForCausalLM", "ExaoneForCausalLM", "FalconForCausalLM", "GemmaForCausalLM",
        "Gemma2ForCausalLM", "Gemma3ForCausalLM", "Gemma3ForConditionalGeneration",
        "GlmForCausalLM", "Glm4ForCausalLM", "Glm4vForConditionalGeneration",
        "GPT2LMHeadModel", "GPTBigCodeForCausalLM", "GPTJForCausalLM", "GPTNeoXForCausalLM",
        "GptOssForCausalLM", "GraniteForCausalLM", "GraniteMoeForCausalLM",
        "InternLM2ForCausalLM", "InternLMForCausalLM", "InternVLChatModel",
        "JAISLMHeadModel", "JambaForCausalLM", "LlamaForCausalLM",
        "Llama4ForConditionalGeneration", "LlavaForConditionalGeneration",
        "LlavaNextForConditionalGeneration", "MiniCPMForCausalLM", "MiniCPM3ForCausalLM",
        "MiniMaxText01ForCausalLM", "MistralForCausalLM", "Mistral3ForConditionalGeneration",
        "MixtralForCausalLM", "MllamaForConditionalGeneration", "MPTForCausalLM",
        "NemotronForCausalLM", "OlmoForCausalLM", "Olmo2ForCausalLM", "OlmoeForCausalLM",
        "OPTForCausalLM", "OrionForCausalLM", "PersimmonForCausalLM", "PhiForCausalLM",
        "Phi3ForCausalLM", "Phi3SmallForCausalLM", "Phi3VForCausalLM", "PhiMoEForCausalLM",
        "PixtralForConditionalGeneration", "QWenLMHeadModel", "Qwen2ForCausalLM",
        "Qwen2MoeForCausalLM", "Qwen2VLForConditionalGeneration",
        "Qwen2_5_VLForConditionalGeneration", "Qwen3ForCausalLM", "Qwen3MoeForCausalLM",
        "Qwen3VLForConditionalGeneration", "Qwen3VLMoeForConditionalGeneration",
        "SolarForCausalLM", "StableLmForCausalLM", "Starcoder2ForCausalLM",
        "TeleChat2ForCausalLM", "XverseForCausalLM", "Zamba2ForCausalLM",
    }
)

SGLANG_ARCHITECTURES: frozenset[str] = frozenset(
    {
        "BaichuanForCausalLM", "ChatGLMModel", "CohereForCausalLM", "DbrxForCausalLM",
        "DeepseekForCausalLM", "DeepseekV2ForCausalLM", "DeepseekV3ForCausalLM",
        "ExaoneForCausalLM", "GemmaForCausalLM", "Gemma2ForCausalLM", "Gemma3ForCausalLM",
        "Gemma3ForConditionalGeneration", "GlmForCausalLM", "Glm4ForCausalLM",
        "GPT2LMHeadModel", "GPTBigCodeForCausalLM", "GptOssForCausalLM",
        "GraniteForCausalLM", "Grok1ForCausalLM", "InternLM2ForCausalLM",
        "LlamaForCausalLM", "Llama4ForConditionalGeneration",
        "LlavaForConditionalGeneration", "MiniCPMForCausalLM", "MiniCPM3ForCausalLM",
        "MistralForCausalLM", "MixtralForCausalLM", "MllamaForConditionalGeneration",
        "OlmoForCausalLM", "Olmo2ForCausalLM", "Phi3ForCausalLM", "PhiMoEForCausalLM",
        "QWenLMHeadModel", "Qwen2ForCausalLM", "Qwen2MoeForCausalLM",
        "Qwen2VLForConditionalGeneration", "Qwen2_5_VLForConditionalGeneration",
        "Qwen3ForCausalLM", "Qwen3MoeForCausalLM", "StableLmForCausalLM",
        "Starcoder2ForCausalLM", "XverseForCausalLM",
    }
)

#: Quantization support per runtime. Anything absent is unsupported.
_VLLM_QUANTS: dict[str, SupportLevel] = {
    "fp32": SupportLevel.SUPPORTED, "fp16": SupportLevel.SUPPORTED,
    "bf16": SupportLevel.SUPPORTED, "fp8": SupportLevel.SUPPORTED,
    "int8": SupportLevel.SUPPORTED, "awq_int4": SupportLevel.SUPPORTED,
    "gptq_int4": SupportLevel.SUPPORTED, "int4": SupportLevel.SUPPORTED,
    "nf4": SupportLevel.SUPPORTED, "mxfp4": SupportLevel.SUPPORTED,
    "nvfp4": SupportLevel.SUPPORTED,
    "q8_0": SupportLevel.UNVERIFIED, "q6_k": SupportLevel.UNVERIFIED,
    "q5_k_m": SupportLevel.UNVERIFIED, "q4_k_m": SupportLevel.UNVERIFIED,
    "q4_0": SupportLevel.UNVERIFIED, "q3_k_m": SupportLevel.UNVERIFIED,
    "q2_k": SupportLevel.UNVERIFIED,
}

_SGLANG_QUANTS: dict[str, SupportLevel] = {
    "fp32": SupportLevel.SUPPORTED, "fp16": SupportLevel.SUPPORTED,
    "bf16": SupportLevel.SUPPORTED, "fp8": SupportLevel.SUPPORTED,
    "int8": SupportLevel.SUPPORTED, "awq_int4": SupportLevel.SUPPORTED,
    "gptq_int4": SupportLevel.SUPPORTED, "int4": SupportLevel.SUPPORTED,
    "mxfp4": SupportLevel.SUPPORTED, "nvfp4": SupportLevel.SUPPORTED,
    "nf4": SupportLevel.UNSUPPORTED,
    "q8_0": SupportLevel.UNSUPPORTED, "q6_k": SupportLevel.UNSUPPORTED,
    "q5_k_m": SupportLevel.UNSUPPORTED, "q4_k_m": SupportLevel.UNSUPPORTED,
    "q4_0": SupportLevel.UNSUPPORTED, "q3_k_m": SupportLevel.UNSUPPORTED,
    "q2_k": SupportLevel.UNSUPPORTED,
}


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    architectures: frozenset[str]
    quants: dict[str, SupportLevel]


RUNTIMES: dict[str, RuntimeProfile] = {
    "vllm": RuntimeProfile("vllm", VLLM_ARCHITECTURES, _VLLM_QUANTS),
    "sglang": RuntimeProfile("sglang", SGLANG_ARCHITECTURES, _SGLANG_QUANTS),
}


#: GGUF names its architectures in ggml's lowercase style. Map them onto the
#: transformers class names the support lists are keyed by.
GGUF_ARCHITECTURE_NAMES: dict[str, str] = {
    "llama": "LlamaForCausalLM",
    "llama4": "Llama4ForConditionalGeneration",
    "mistral": "MistralForCausalLM",
    "mixtral": "MixtralForCausalLM",
    "qwen2": "Qwen2ForCausalLM",
    "qwen2moe": "Qwen2MoeForCausalLM",
    "qwen3": "Qwen3ForCausalLM",
    "qwen3moe": "Qwen3MoeForCausalLM",
    "gemma": "GemmaForCausalLM",
    "gemma2": "Gemma2ForCausalLM",
    "gemma3": "Gemma3ForCausalLM",
    "phi2": "PhiForCausalLM",
    "phi3": "Phi3ForCausalLM",
    "gpt2": "GPT2LMHeadModel",
    "gptoss": "GptOssForCausalLM",
    "gpt-oss": "GptOssForCausalLM",
    "starcoder2": "Starcoder2ForCausalLM",
    "falcon": "FalconForCausalLM",
    "stablelm": "StableLmForCausalLM",
    "olmo": "OlmoForCausalLM",
    "olmo2": "Olmo2ForCausalLM",
    "minicpm": "MiniCPMForCausalLM",
    "command-r": "CohereForCausalLM",
    "cohere2": "Cohere2ForCausalLM",
    "deepseek2": "DeepseekV2ForCausalLM",
    "deepseek3": "DeepseekV3ForCausalLM",
    "internlm2": "InternLM2ForCausalLM",
    "granite": "GraniteForCausalLM",
    "chatglm": "ChatGLMModel",
    "bloom": "BloomForCausalLM",
    "dbrx": "DbrxForCausalLM",
    "exaone": "ExaoneForCausalLM",
    "glm4": "Glm4ForCausalLM",
}


def normalize_architecture(name: str) -> str:
    """Accept either a transformers class name or a ggml architecture name."""
    return GGUF_ARCHITECTURE_NAMES.get(name.strip().lower(), name)


def quant_requirement(dtype: str) -> QuantRequirement:
    info = quant_info(dtype)
    return QuantRequirement(
        dtype=info.key,
        native_compute_capability=info.native_compute_capability,
        emulated_below_native=info.emulated_below_native,
        note=info.note,
    )


def evaluate_runtime(
    runtime: str, architectures: tuple[str, ...], dtype: str
) -> RuntimeSupport:
    """Architecture and quantization support for one runtime."""
    key = runtime.strip().lower()
    profile = RUNTIMES.get(key)
    if profile is None:
        return RuntimeSupport(key, SupportLevel.UNVERIFIED, f"unknown runtime {runtime!r}")

    architectures = tuple(normalize_architecture(a) for a in architectures)
    quant_level = profile.quants.get(dtype, SupportLevel.UNSUPPORTED)
    if quant_level is SupportLevel.UNSUPPORTED:
        return RuntimeSupport(
            key,
            SupportLevel.UNSUPPORTED,
            f"{profile.name} cannot load {dtype} weights"
            + (
                "; GGUF is llama.cpp's format and belongs on a llama.cpp backend"
                if quant_info(dtype).family == "gguf"
                else ""
            ),
        )

    known = [a for a in architectures if a in profile.architectures]
    if not architectures:
        arch_level, arch_reason = (
            SupportLevel.UNVERIFIED,
            "no architecture on record for this shape, so runtime support is "
            "unverified; resolve the model id first and the check becomes exact",
        )
    elif known:
        arch_level, arch_reason = (
            SupportLevel.SUPPORTED,
            f"{profile.name} supports {known[0]}",
        )
    else:
        arch_level, arch_reason = (
            SupportLevel.UNSUPPORTED,
            f"{architectures[0]} is not in {profile.name}'s supported architecture list",
        )

    if arch_level is SupportLevel.UNSUPPORTED:
        return RuntimeSupport(key, SupportLevel.UNSUPPORTED, arch_reason)
    if quant_level is SupportLevel.UNVERIFIED:
        return RuntimeSupport(
            key,
            SupportLevel.UNVERIFIED,
            f"{arch_reason}, but {profile.name}'s {dtype} path is experimental",
        )
    if arch_level is SupportLevel.UNVERIFIED:
        return RuntimeSupport(key, SupportLevel.UNVERIFIED, arch_reason)
    return RuntimeSupport(key, SupportLevel.SUPPORTED, f"{arch_reason} at {dtype}")


def build_verdict(architectures: tuple[str, ...], dtype: str) -> SupportVerdict:
    return SupportVerdict(
        architectures=architectures,
        runtimes=tuple(evaluate_runtime(name, architectures, dtype) for name in RUNTIMES),
        quant=quant_requirement(dtype),
    )


def check_nodes(
    dtype: str, nodes: list[NodeProfile]
) -> tuple[bool, list[str]]:
    """Whether every node can run this quantization scheme.

    MXFP4 is the case that matters here: native on Blackwell, which is what a
    Spark is, dequantized in kernel on Hopper and Ada, and on anything older the
    runtime widens the weights to bf16, which quadruples the footprint the fit
    check was told to expect.
    """
    requirement = quant_requirement(dtype)
    problems: list[str] = []
    ok = True
    for node in nodes:
        passed, reason = requirement.check(node.compute_capability)
        if not passed:
            ok = False
            problems.append(f"{node.node_id} ({node.gpu_name}): {reason}")
        elif "emulated" in reason:
            problems.append(f"{node.node_id} ({node.gpu_name}): {reason}")
    return ok, problems


def sharding_notes(shape: ModelShape, world_size: int) -> list[str]:
    """Divisibility constraints a plan has to respect. Advisory, not a refusal."""
    notes: list[str] = []
    if world_size <= 1:
        return notes
    if shape.num_kv_heads % world_size and world_size % max(shape.num_kv_heads, 1):
        notes.append(
            f"{shape.num_kv_heads} KV heads do not divide evenly by a tensor-parallel "
            f"degree of {world_size}; the runtime will replicate KV heads across ranks"
        )
    if shape.num_attention_heads % world_size:
        notes.append(
            f"{shape.num_attention_heads} attention heads are not divisible by "
            f"{world_size}, which tensor parallel requires"
        )
    if shape.is_moe and shape.num_experts % world_size:
        notes.append(
            f"{shape.num_experts} experts are not divisible by {world_size}, so expert "
            "parallel would leave ranks unevenly loaded"
        )
    return notes
