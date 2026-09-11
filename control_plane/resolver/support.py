"""Can the runtime actually load this?

Weights fitting and the runtime supporting the architecture are different
questions, and the second one is the cheaper to answer. A model that clears the
byte check and fails here should be refused before launch rather than at load
time, five minutes in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from control_plane.contracts import ENDPOINT_FOR_MODALITY, Modality, ModelShape, NodeProfile
from control_plane.contracts.quant import quant_info

from .types import QuantRequirement, RuntimeSupport, SupportLevel, SupportVerdict

#: Architectures the vllm runtime can load, read out of the image this project
#: launches -- ghcr.io/spark-arena/dgx-vllm-eugr-nightly, vLLM
#: 0.28.1rc1.dev462+g9ca97b28b.d20260906, torch 2.13.0+cu130, transformers
#: 5.16.1, read on 2026-09-07.
#:
#: Generated from that image rather than curated by hand, because a hand-kept
#: list goes stale in the one direction nobody notices: it keeps refusing
#: models that started working. `Gemma4ForConditionalGeneration` was refused
#: with "not in vllm's supported architecture list" while the pinned image had
#: been able to load it all along. An absent name is not a hedge here --
#: `evaluate_runtime` returns UNSUPPORTED, `RuntimeSupport.ok` is False, and
#: `internal_api` turns that into a 400 `runtime_unsupported` -- so every name
#: missing from this set is a model this build will not launch.
#:
#: Regenerate against a new image, and paste what it prints:
#:
#:     docker run --rm --entrypoint python3 "$DERATE_VLLM_IMAGE" -c '
#:     import vllm.model_executor.models.registry as r
#:     cat = lambda n: set(getattr(r, n))
#:     pooling = (cat("_EMBEDDING_MODELS") | cat("_REWARD_MODELS")
#:                | cat("_SEQUENCE_CLASSIFICATION_MODELS")
#:                | cat("_TOKEN_CLASSIFICATION_MODELS")
#:                | cat("_LATE_INTERACTION_MODELS"))
#:     generative = cat("_TEXT_GENERATION_MODELS") | cat("_MULTIMODAL_MODELS")
#:     print(sorted(set(r._VLLM_MODELS) - cat("_SPECULATIVE_DECODING_MODELS")
#:                  - cat("_TRANSFORMERS_BACKEND_MODELS") - (pooling - generative)))'
#:
#: That is the registry's servable set, and each subtraction is a claim this
#: table would otherwise make wrongly. Draft heads go: `Gemma4MTPModel` and
#: `Gemma4DSparkModel` are speculators a real model loads, not servers. The
#: generic transformers-backend wrappers go: `TransformersForCausalLM` is an
#: implementation vLLM picks, never a name in a checkpoint's config.json.
#: Pooling-only models go -- embedding, reward, classification -- but only
#: those with no generative path, since vLLM registers `GlmForCausalLM` and
#: `DeciLMForCausalLM` in both places and they chat. Anything vLLM has since
#: dropped (`_PREVIOUSLY_SUPPORTED_MODELS`) is absent by construction, which
#: is how thirteen names left this set: `MllamaForConditionalGeneration` --
#: Llama 3.2 Vision -- was listed here, and this image cannot load it.
#:
#: Loadable is the only claim the set makes. Speech models are in it too, and
#: vLLM answers those on /v1/audio/transcriptions rather than
#: /v1/chat/completions; AUDIO_ARCHITECTURES below is what decides the route.
#: Being loadable and being answerable on the chat route are separate claims,
#: and keeping them separate is what stops a transcription-only checkpoint
#: from being offered as a chat model.
VLLM_ARCHITECTURES: frozenset[str] = frozenset(
    {
        "AXK1ForCausalLM", "AfmoeForCausalLM", "ApertusForCausalLM", "ArceeForCausalLM",
        "AriaForConditionalGeneration", "AudioFlamingo3ForConditionalGeneration",
        "BagelForConditionalGeneration", "BailingMoeForCausalLM", "BailingMoeV2ForCausalLM",
        "BailingMoeV2_5ForCausalLM", "BailingMoeV3ForCausalLM", "BeeForConditionalGeneration",
        "Blip2ForConditionalGeneration", "BloomForCausalLM", "ChatGLMForConditionalGeneration",
        "ChatGLMModel", "Cohere2ForCausalLM", "Cohere2MoeForCausalLM",
        "Cohere2VisionForConditionalGeneration", "CohereAsrForConditionalGeneration",
        "CohereForCausalLM", "Cosmos3EdgeForConditionalGeneration",
        "Cosmos3ForConditionalGeneration", "CwmForCausalLM", "DbrxForCausalLM",
        "DeciLMForCausalLM", "DeepseekForCausalLM", "DeepseekOCR2ForCausalLM",
        "DeepseekOCRForCausalLM", "DeepseekV2ForCausalLM", "DeepseekV32ForCausalLM",
        "DeepseekV3ForCausalLM", "DeepseekV4ForCausalLM", "DeepseekV4ForConditionalGeneration",
        "DeepseekVLV2ForCausalLM", "DiffusionGemmaForBlockDiffusion", "Dots3NoteForCausalLM",
        "DotsOCRForCausalLM", "Eagle2_5_VLForConditionalGeneration",
        "Emu3ForConditionalGeneration", "Ernie4_5ForCausalLM", "Ernie4_5_MoeForCausalLM",
        "Ernie4_5_VLMoeForConditionalGeneration", "Exaone4ForCausalLM",
        "Exaone4_5_ForConditionalGeneration", "ExaoneForCausalLM", "ExaoneMoeForCausalLM",
        "FalconForCausalLM", "FalconH1ForCausalLM", "FalconMambaForCausalLM",
        "FireRedASR2ForConditionalGeneration", "FlexOlmoForCausalLM",
        "FunASRForConditionalGeneration", "FunAudioChatForConditionalGeneration",
        "GLM4VForCausalLM", "GPT2LMHeadModel", "GPTBigCodeForCausalLM", "GPTJForCausalLM",
        "GPTNeoXForCausalLM", "Gemma2ForCausalLM", "Gemma3ForCausalLM",
        "Gemma3ForConditionalGeneration", "Gemma3nForCausalLM",
        "Gemma3nForConditionalGeneration", "Gemma4ForCausalLM",
        "Gemma4ForConditionalGeneration", "Gemma4UnifiedForConditionalGeneration",
        "GemmaForCausalLM", "Glm4ForCausalLM", "Glm4MoeForCausalLM", "Glm4MoeLiteForCausalLM",
        "Glm4vForConditionalGeneration", "Glm4vMoeForConditionalGeneration",
        "Glm5NextForCausalLM", "Glm5NextForConditionalGeneration",
        "GlmAsrForConditionalGeneration", "GlmForCausalLM", "GlmMoeDsaForCausalLM",
        "GlmOcrForConditionalGeneration", "GptOssForCausalLM",
        "Granite4VisionForConditionalGeneration", "GraniteForCausalLM", "GraniteMoeForCausalLM",
        "GraniteMoeHybridForCausalLM", "GraniteMoeSWAForCausalLM",
        "GraniteMoeSharedForCausalLM", "GraniteSWAForCausalLM",
        "GraniteSpeechForConditionalGeneration", "GraniteSpeechPlusForConditionalGeneration",
        "H2OVLChatModel", "HCXVisionV2ForCausalLM", "HYV3ForCausalLM", "HYV4ForCausalLM",
        "HfMoondream", "HrmTextForCausalLM", "HunYuanDenseV1ForCausalLM",
        "HunYuanMoEV1ForCausalLM", "HunYuanVLForConditionalGeneration",
        "HyperCLOVAXForCausalLM", "IQuestCoderForCausalLM", "IQuestLoopCoderForCausalLM",
        "Idefics3ForConditionalGeneration", "InklingForCausalLM",
        "InklingForConditionalGeneration", "InternLM2ForCausalLM", "InternLM3ForCausalLM",
        "InternS1ForConditionalGeneration", "InternS1ProForConditionalGeneration",
        "InternS2MobiusForConditionalGeneration", "InternS2PreviewForConditionalGeneration",
        "InternVLChatModel", "InternVLForConditionalGeneration",
        "IsaacForConditionalGeneration", "Jais2ForCausalLM", "JambaForCausalLM",
        "K2HorizonForCausalLM", "KananaVForConditionalGeneration",
        "KeyeForConditionalGeneration", "KeyeVL1_5ForConditionalGeneration",
        "KimiK25ForConditionalGeneration", "KimiK3ForConditionalGeneration",
        "KimiLinearForCausalLM", "KimiVLForConditionalGeneration", "LLaMAForCausalLM",
        "LagunaForCausalLM", "Lfm2ForCausalLM", "Lfm2MoeForCausalLM",
        "Lfm2VlForConditionalGeneration", "LightOnOCRForConditionalGeneration",
        "Llama4ForCausalLM", "Llama4ForConditionalGeneration", "LlamaForCausalLM",
        "Llama_Nemotron_Nano_VL", "LlavaForConditionalGeneration",
        "LlavaNextForConditionalGeneration", "LlavaNextVideoForConditionalGeneration",
        "LlavaOnevision2ForConditionalGeneration", "LlavaOnevisionForConditionalGeneration",
        "LongcatFlashForCausalLM", "LongcatFlashNgramForCausalLM", "Mamba2ForCausalLM",
        "MambaForCausalLM", "MellumForCausalLM", "MiDashengLMModel", "MiMoForCausalLM",
        "MiMoV2FlashForCausalLM", "MiMoV2ForCausalLM", "MiMoV2OmniForCausalLM",
        "MiniCPM3ForCausalLM", "MiniCPMForCausalLM", "MiniCPMO", "MiniCPMV",
        "MiniCPMV4_6ForConditionalGeneration", "MiniMaxM2ForCausalLM",
        "MiniMaxM3SparseForCausalLM", "MiniMaxM3SparseForConditionalGeneration",
        "Ministral3ForCausalLM", "Mistral3ForConditionalGeneration", "MistralForCausalLM",
        "MistralLarge3ForCausalLM", "MixtralForCausalLM", "Molmo2ForConditionalGeneration",
        "MolmoForCausalLM", "Moondream3ForCausalLM", "MoonshotKimiaForCausalLM",
        "MossAudioModel", "MossTranscribeDiarizeForConditionalGeneration",
        "MuseGlimmerForCausalLM", "MuseGlimmerForConditionalGeneration", "NVLM_D",
        "NemotronForCausalLM", "NemotronHForCausalLM", "NemotronHPuzzleForCausalLM",
        "NemotronH_Nano_Omni_Reasoning_V3", "NemotronH_Nano_VL_V2",
        "NemotronH_Omni_Reasoning_V3", "NemotronH_Super_Omni_Reasoning_V3",
        "NemotronParseForConditionalGeneration", "OPTForCausalLM", "Olmo2ForCausalLM",
        "Olmo3ForCausalLM", "OlmoForCausalLM", "OlmoHybridForCausalLM", "OlmoeForCausalLM",
        "OpenCUAForConditionalGeneration", "OpenPanguVLForConditionalGeneration",
        "OpenVLAForActionPrediction", "OrionForCausalLM", "Ovis", "Ovis2_5",
        "Ovis2_6ForCausalLM", "Ovis2_6_MoeForCausalLM", "PaddleOCRVLForConditionalGeneration",
        "PaliGemmaForConditionalGeneration", "PanguEmbeddedForCausalLM",
        "PanguProMoEV2ForCausalLM", "PanguUltraMoEForCausalLM", "Param2MoEForCausalLM",
        "Phi3ForCausalLM", "Phi3VForCausalLM", "Phi4ForCausalLMV", "Phi4MMForCausalLM",
        "PhiForCausalLM", "PhiMoEForCausalLM", "PixtralForConditionalGeneration",
        "Plamo3ForCausalLM", "QianfanOCRForConditionalGeneration",
        "Qwen2AudioForConditionalGeneration", "Qwen2ForCausalLM", "Qwen2MoeForCausalLM",
        "Qwen2VLForConditionalGeneration", "Qwen2_5OmniForConditionalGeneration",
        "Qwen2_5OmniModel", "Qwen2_5_VLForConditionalGeneration",
        "Qwen3ASRForConditionalGeneration", "Qwen3ASRRealtimeGeneration", "Qwen3ForCausalLM",
        "Qwen3MoeForCausalLM", "Qwen3NextForCausalLM", "Qwen3OmniMoeForConditionalGeneration",
        "Qwen3VLForConditionalGeneration", "Qwen3VLMoeForConditionalGeneration",
        "Qwen3_5ForCausalLM", "Qwen3_5ForConditionalGeneration", "Qwen3_5MoeForCausalLM",
        "Qwen3_5MoeForConditionalGeneration", "Qwen4ExpForCausalLM",
        "Qwen4ExpForConditionalGeneration", "RForConditionalGeneration", "Rnj1ForCausalLM",
        "SarvamMLAForCausalLM", "SarvamMoEForCausalLM", "SeedOssForCausalLM",
        "SkyworkR1VChatModel", "SmolLM3ForCausalLM", "SmolVLMForConditionalGeneration",
        "SolarForCausalLM", "StableLmForCausalLM", "Starcoder2ForCausalLM", "Step1ForCausalLM",
        "Step3TextForCausalLM", "Step3VLForConditionalGeneration", "Step3p5ForCausalLM",
        "Step3p7ForConditionalGeneration", "StepVLForConditionalGeneration",
        "TeleChat2ForCausalLM", "TeleChat3ForCausalLM", "TeleFLMForCausalLM", "UltravoxModel",
        "UnlimitedOCRForCausalLM", "VaultGemmaForCausalLM",
        "VibeVoiceAsrForConditionalGeneration", "VoxtralForConditionalGeneration",
        "VoxtralRealtimeGeneration", "WhisperForConditionalGeneration", "Zamba2ForCausalLM",
    }
)

#: In the registry above, and empirically not the same claim as loadable.
#: Being registered only means the class exists in vllm's model executor;
#: nothing about that proves a forward pass completes on this build. This
#: table is for the gap between those two claims, and it is populated the same
#: way the registry itself is kept honest -- somebody watched a launch die and
#: wrote down what it said, never a guess about what might be shaky.
#:
#: Keyed by architecture, not by model id: the failure lives in the runtime's
#: code for that class, not in any one checkpoint's weights, so every model
#: that resolves to this architecture inherits the same crash. A version is
#: named in the value because the underlying bug may be fixed by a later
#: image; re-check before trusting an old entry here against a new one.
VLLM_KNOWN_BROKEN: dict[str, str] = {
    "DiffusionGemmaForBlockDiffusion": (
        "gets past every earlier gate -- weights load, torch.compile "
        "succeeds -- and dies in CUDA graph capture warmup: "
        "vllm/model_executor/models/diffusion_gemma.py's prepare_attn hands "
        "FlashInfer's prefill plan() a tensor-shaped causal mask for this "
        "architecture's mixed causal/bidirectional attention, and this "
        "build's compiled binding rejects it -- \"TypeError: Mismatched type "
        "on argument #14 ... Expected `bool` but got `ffi.Tensor`\". vLLM's "
        "own backend selector logs intent to exclude FlashInfer for this "
        "architecture and picks it anyway. Observed on 4 consecutive "
        "launches of google/diffusiongemma-26B-A4B-it against vllm "
        "0.28.1rc1.dev486+gd875ff5ba.d20260907, 2026-09-07."
    ),
}

#: SGLang's, hand-kept and confirmed stale against a real image: the pinned
#: tag is scitrera/dgx-spark-sglang:0.5.9-t5, but scitrera/dgx-spark-sglang:0.5.12
#: was available to check, and its registry has both
#: `Gemma4ForConditionalGeneration` and `Gemma4AssistantForCausalLM` -- neither
#: of which is in this set. `imageprobe.py`'s SGLang script (added once that
#: image existed to read) is what closes this now, the same way the vllm probe
#: replaces the set above: `architectures_for("sglang")` prefers a probe when
#: one has run and falls back to this set otherwise. This table is therefore
#: only what a coordinator with no docker socket, or the exact pinned tag
#: un-probed, answers from -- and it is still stale in the direction that
#: refuses working models, so a name added here by hand has to come from a
#: launch somebody watched, same as ever.
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

#: Architectures ``control_plane/runtimes/tts.py`` can load and drive.
#:
#: Narrower than "text-to-speech models", and deliberately so. That server is a
#: transformers loader that calls exactly three things on the checkpoint --
#: ``processor(text=..., reference_audio=..., reference_text=...)``,
#: ``model.generate() -> codes`` and ``model.decode_audio(codes)`` -- so what
#: belongs here is not "a TTS model" but "a checkpoint whose own remote code
#: answers those three calls". A name goes in after somebody has run it
#: through that server, never because the model card says TTS: a model listed
#: here and not loadable is a launch that passes every gate and dies at load,
#: which is the failure the support table exists to prevent.
TTS_ARCHITECTURES: frozenset[str] = frozenset(
    {
        # DualAR: a slow transformer emitting one semantic token per audio
        # frame, a fast one emitting that frame's codebooks, and a bundled
        # 44.1 kHz codec. Verified on a GB10 against
        # Audio8/Audio8-TTS-Preview-0.6b.
        "ArkttsModel",
    }
)

#: Architectures this build knows are speech models, and which endpoint family
#: each answers. The resolver reports it; the deployment manager records it on
#: the Deployment so the gateway can route on it. Absent means text, which is
#: what every model here was before audio existed.
AUDIO_ARCHITECTURES: dict[str, str] = {
    "WhisperForConditionalGeneration": "transcription",
    "Qwen2AudioForConditionalGeneration": "transcription",
    "VoxtralForConditionalGeneration": "transcription",
    # The rest of the transcription-only architectures the pinned vLLM image
    # carries, added with the VLLM_ARCHITECTURES sync above and for the same
    # reason: that set now claims every ASR model vLLM registers is loadable,
    # and a loadable speech model with no row here reads as text and gets
    # offered on /v1/chat/completions, which is a launch that clears every
    # gate and fails on the first request.
    #
    # Only the ones vLLM marks `supports_transcription_only` are here. The
    # image also loads GlmAsr, GraniteSpeech, Qwen3ASR, Qwen3OmniMoe,
    # MoonshotKimia and Gemma3n, all of which transcribe *and* chat; those
    # stay on the chat route, because taking a chat model off it is the
    # louder failure of the two.
    "CohereAsrForConditionalGeneration": "transcription",
    "FireRedASR2ForConditionalGeneration": "transcription",
    "FunASRForConditionalGeneration": "transcription",
    "MossTranscribeDiarizeForConditionalGeneration": "transcription",
    # NeMo's Parakeet TDT, transformers-native port (nvidia/parakeet-tdt-0.6b-v3
    # ships it; v2 predates the port and is NeMo-only, no config.json).
    "ParakeetForTDT": "transcription",
    # Synthesis, the other direction: text in, audio out, answered on
    # /v1/audio/speech. Every architecture the tts runtime loads is one of
    # these by construction -- that runtime serves no other route -- so the
    # two sets are kept in step below rather than typed twice.
    **{name: "speech" for name in TTS_ARCHITECTURES},
}


def modality_for(architectures) -> str:
    """Which endpoint family these architectures answer on.

    A string rather than the Modality enum so `resolver/` keeps its existing
    independence from `contracts/` beyond ModelShape; the caller converts.
    """
    for name in architectures or ():
        found = AUDIO_ARCHITECTURES.get(name)
        if found:
            return found
    return "text"

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


#: What llama.cpp loads, derived from the table above rather than hand-kept.
#:
#: Every key in `GGUF_ARCHITECTURE_NAMES` is a ggml architecture name, and a
#: ggml architecture name exists only because llama.cpp has a converter and a
#: graph for that family -- that is what the name IS. So the values of that
#: map are not a claim copied off a README, they are the same evidence the
#: mapping itself rests on, and a new entry there teaches this runtime the
#: architecture in the same edit.
#:
#: It is narrower than llama.cpp's real coverage, which is the safe direction:
#: an architecture missing here is refused with `_elsewhere()` naming a
#: runtime that does load it, while one wrongly present is a launch that
#: clears every gate and dies inside a container on another machine.
LLAMACPP_ARCHITECTURES: frozenset[str] = frozenset(GGUF_ARCHITECTURE_NAMES.values())


#: Quantization support per runtime. Anything absent is unsupported.
_VLLM_QUANTS: dict[str, SupportLevel] = {
    "fp32": SupportLevel.SUPPORTED, "fp16": SupportLevel.SUPPORTED,
    "bf16": SupportLevel.SUPPORTED, "fp8": SupportLevel.SUPPORTED,
    "int8": SupportLevel.SUPPORTED, "awq_int4": SupportLevel.SUPPORTED,
    "gptq_int4": SupportLevel.SUPPORTED,
    "nf4": SupportLevel.SUPPORTED, "mxfp4": SupportLevel.SUPPORTED,
    "nvfp4": SupportLevel.SUPPORTED,
    "q8_0": SupportLevel.UNVERIFIED, "q6_k": SupportLevel.UNVERIFIED,
    "q5_k_m": SupportLevel.UNVERIFIED, "q4_k_m": SupportLevel.UNVERIFIED,
    "q4_0": SupportLevel.UNVERIFIED, "q3_k_m": SupportLevel.UNVERIFIED,
    "q2_k": SupportLevel.UNVERIFIED,
    # The rest of the llama.cpp ladder, at the same level as the K-quants
    # above: vLLM has a GGUF loader, we have not verified these on it. An
    # absent key would default to UNSUPPORTED, which would make iq4_xs read
    # as stricter than q4_k_m for no reason anyone could defend.
    "q4_1": SupportLevel.UNVERIFIED, "q5_0": SupportLevel.UNVERIFIED, "q5_1": SupportLevel.UNVERIFIED,
    "q2_k_s": SupportLevel.UNVERIFIED, "iq1_s": SupportLevel.UNVERIFIED, "iq1_m": SupportLevel.UNVERIFIED,
    "iq2_xxs": SupportLevel.UNVERIFIED, "iq2_xs": SupportLevel.UNVERIFIED, "iq2_s": SupportLevel.UNVERIFIED,
    "iq2_m": SupportLevel.UNVERIFIED, "iq3_xxs": SupportLevel.UNVERIFIED, "iq3_xs": SupportLevel.UNVERIFIED,
    "iq3_s": SupportLevel.UNVERIFIED, "iq3_m": SupportLevel.UNVERIFIED, "iq4_xs": SupportLevel.UNVERIFIED,
    "iq4_nl": SupportLevel.UNVERIFIED,
}

_SGLANG_QUANTS: dict[str, SupportLevel] = {
    "fp32": SupportLevel.SUPPORTED, "fp16": SupportLevel.SUPPORTED,
    "bf16": SupportLevel.SUPPORTED, "fp8": SupportLevel.SUPPORTED,
    "int8": SupportLevel.SUPPORTED, "awq_int4": SupportLevel.SUPPORTED,
    "gptq_int4": SupportLevel.SUPPORTED,
    "mxfp4": SupportLevel.SUPPORTED, "nvfp4": SupportLevel.SUPPORTED,
    "nf4": SupportLevel.UNSUPPORTED,
    "q8_0": SupportLevel.UNSUPPORTED, "q6_k": SupportLevel.UNSUPPORTED,
    "q5_k_m": SupportLevel.UNSUPPORTED, "q4_k_m": SupportLevel.UNSUPPORTED,
    "q4_0": SupportLevel.UNSUPPORTED, "q3_k_m": SupportLevel.UNSUPPORTED,
    "q2_k": SupportLevel.UNSUPPORTED,
    # SGLang has no GGUF path at all, so the whole ladder is refused --
    # stated explicitly rather than left to the default, so the table reads
    # as a decision rather than an omission.
    "q4_1": SupportLevel.UNSUPPORTED, "q5_0": SupportLevel.UNSUPPORTED, "q5_1": SupportLevel.UNSUPPORTED,
    "q2_k_s": SupportLevel.UNSUPPORTED, "iq1_s": SupportLevel.UNSUPPORTED, "iq1_m": SupportLevel.UNSUPPORTED,
    "iq2_xxs": SupportLevel.UNSUPPORTED, "iq2_xs": SupportLevel.UNSUPPORTED, "iq2_s": SupportLevel.UNSUPPORTED,
    "iq2_m": SupportLevel.UNSUPPORTED, "iq3_xxs": SupportLevel.UNSUPPORTED, "iq3_xs": SupportLevel.UNSUPPORTED,
    "iq3_s": SupportLevel.UNSUPPORTED, "iq3_m": SupportLevel.UNSUPPORTED, "iq4_xs": SupportLevel.UNSUPPORTED,
    "iq4_nl": SupportLevel.UNSUPPORTED,
}


#: The tts runtime loads through transformers, in the checkpoint's own dtype.
#: There is no quantization path at all: no GPTQ or AWQ kernel is involved,
#: and GGUF is llama.cpp's format for a llama.cpp backend. Stated key by key
#: rather than left to the default, so the table reads as a decision.
_TTS_QUANTS: dict[str, SupportLevel] = {
    "fp32": SupportLevel.SUPPORTED,
    "fp16": SupportLevel.SUPPORTED,
    "bf16": SupportLevel.SUPPORTED,
    "fp8": SupportLevel.UNSUPPORTED,
    "int8": SupportLevel.UNSUPPORTED,
    "awq_int4": SupportLevel.UNSUPPORTED,
    "gptq_int4": SupportLevel.UNSUPPORTED,
    "nf4": SupportLevel.UNSUPPORTED,
    "mxfp4": SupportLevel.UNSUPPORTED,
    "nvfp4": SupportLevel.UNSUPPORTED,
    "q8_0": SupportLevel.UNSUPPORTED, "q6_k": SupportLevel.UNSUPPORTED,
    "q5_k_m": SupportLevel.UNSUPPORTED, "q4_k_m": SupportLevel.UNSUPPORTED,
    "q4_0": SupportLevel.UNSUPPORTED, "q3_k_m": SupportLevel.UNSUPPORTED,
    "q2_k": SupportLevel.UNSUPPORTED,
    "q4_1": SupportLevel.UNSUPPORTED, "q5_0": SupportLevel.UNSUPPORTED,
    "q5_1": SupportLevel.UNSUPPORTED, "q2_k_s": SupportLevel.UNSUPPORTED,
    "iq1_s": SupportLevel.UNSUPPORTED, "iq1_m": SupportLevel.UNSUPPORTED,
    "iq2_xxs": SupportLevel.UNSUPPORTED, "iq2_xs": SupportLevel.UNSUPPORTED,
    "iq2_s": SupportLevel.UNSUPPORTED, "iq2_m": SupportLevel.UNSUPPORTED,
    "iq3_xxs": SupportLevel.UNSUPPORTED, "iq3_xs": SupportLevel.UNSUPPORTED,
    "iq3_s": SupportLevel.UNSUPPORTED, "iq3_m": SupportLevel.UNSUPPORTED,
    "iq4_xs": SupportLevel.UNSUPPORTED, "iq4_nl": SupportLevel.UNSUPPORTED,
}


#: llama.cpp reads GGUF and nothing else, so this is the first table here
#: where the ladder the other three refuse is the ladder that works -- and the
#: first where `fp16`/`bf16` are UNSUPPORTED rather than the easy yes.
#:
#: That pair is the one worth arguing. llama.cpp can hold F16 tensors, so
#: "cannot load fp16" reads wrong at first glance. What it cannot do is read a
#: safetensors repository, and `fp16` on a shape here overwhelmingly means
#: exactly that -- an unquantized checkpoint in somebody's normal weights
#: format. Saying SUPPORTED would clear a launch that dies at load, which is
#: the failure this project spends the most effort refusing to produce.
#:
#: The cost is real and is stated rather than hidden: an all-F16 *GGUF* build
#: is refused too, because nothing on a `ModelShape` distinguishes it from the
#: safetensors case -- `evaluate_runtime` is handed a dtype and an
#: architecture and no answer to "is this a GGUF repository". That is a gap in
#: what a shape carries, not a fact about llama.cpp, and the refusal points at
#: a quantized build rather than pretending the model is unservable.
_LLAMACPP_QUANTS: dict[str, SupportLevel] = {
    "fp32": SupportLevel.UNSUPPORTED,
    "fp16": SupportLevel.UNSUPPORTED,
    "bf16": SupportLevel.UNSUPPORTED,
    "fp8": SupportLevel.UNSUPPORTED,
    "int8": SupportLevel.UNSUPPORTED,
    "awq_int4": SupportLevel.UNSUPPORTED,
    "gptq_int4": SupportLevel.UNSUPPORTED,
    "nf4": SupportLevel.UNSUPPORTED,
    "mxfp4": SupportLevel.UNSUPPORTED,
    "nvfp4": SupportLevel.UNSUPPORTED,
    # The ladder, and llama.cpp is where it comes from: these are its own
    # scheme names, produced by its own quantizer.
    "q8_0": SupportLevel.SUPPORTED, "q6_k": SupportLevel.SUPPORTED,
    "q5_k_m": SupportLevel.SUPPORTED, "q4_k_m": SupportLevel.SUPPORTED,
    "q4_0": SupportLevel.SUPPORTED, "q3_k_m": SupportLevel.SUPPORTED,
    "q2_k": SupportLevel.SUPPORTED,
    "q4_1": SupportLevel.SUPPORTED, "q5_0": SupportLevel.SUPPORTED,
    "q5_1": SupportLevel.SUPPORTED, "q2_k_s": SupportLevel.SUPPORTED,
    "iq1_s": SupportLevel.SUPPORTED, "iq1_m": SupportLevel.SUPPORTED,
    "iq2_xxs": SupportLevel.SUPPORTED, "iq2_xs": SupportLevel.SUPPORTED,
    "iq2_s": SupportLevel.SUPPORTED, "iq2_m": SupportLevel.SUPPORTED,
    "iq3_xxs": SupportLevel.SUPPORTED, "iq3_xs": SupportLevel.SUPPORTED,
    "iq3_s": SupportLevel.SUPPORTED, "iq3_m": SupportLevel.SUPPORTED,
    "iq4_xs": SupportLevel.SUPPORTED, "iq4_nl": SupportLevel.SUPPORTED,
}


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    architectures: frozenset[str]
    quants: dict[str, SupportLevel]
    #: Registered but empirically broken, architecture -> what was observed.
    #: Empty for every runtime but vllm today, because nobody has watched
    #: sglang or tts fail this way yet.
    known_broken: dict[str, str] = field(default_factory=dict)


RUNTIMES: dict[str, RuntimeProfile] = {
    "vllm": RuntimeProfile("vllm", VLLM_ARCHITECTURES, _VLLM_QUANTS, VLLM_KNOWN_BROKEN),
    "sglang": RuntimeProfile("sglang", SGLANG_ARCHITECTURES, _SGLANG_QUANTS),
    # The third runtime, and the only one derate wrote itself. It exists
    # because neither of the two above serves /v1/audio/speech, so a
    # text-to-speech checkpoint had nowhere to run at all -- not "ran badly",
    # nowhere. control_plane/runtimes/tts.py.
    "tts": RuntimeProfile("tts", TTS_ARCHITECTURES, _TTS_QUANTS),
    # The fourth, and the only one that does not need a GPU. It exists for the
    # machines the other three cannot use at all -- a Raspberry Pi, a NAS, a
    # spare x86 box -- which until now could join the roster and serve nothing.
    "llamacpp": RuntimeProfile("llamacpp", LLAMACPP_ARCHITECTURES, _LLAMACPP_QUANTS),
}


#: What a runtime image said about itself, when one has been asked.
#:
#: The frozensets above are a hand-copied claim about somebody else's
#: software and they go stale in the one direction nobody notices: they keep
#: refusing models that started working. `imageprobe.py` reads vLLM's own
#: registry out of the image this build launches, and what it finds is
#: recorded here and preferred over the static table for every question this
#: module answers.
#:
#: Empty is the normal state on a machine with no docker or no image pulled,
#: and it costs nothing but the older answer -- which is why `probed` is a
#: lookup with a fallback rather than a branch every caller has to remember.
_PROBED: dict[str, "object"] = {}


def record_probe(found) -> None:
    """Adopt an `imageprobe.ImageProbe`, replacing any earlier one.

    Duck-typed rather than imported so `support` keeps its independence: this
    module is imported by the fit and launch paths on every node, and the
    probe is a coordinator-side convenience that not every one of them has a
    docker socket to run.
    """
    _PROBED[found.runtime.strip().lower()] = found


def clear_probes() -> None:
    _PROBED.clear()


def probed(runtime: str):
    """The probe for *runtime*, or None when nobody has asked its image."""
    return _PROBED.get(runtime.strip().lower())


def architectures_for(runtime: str) -> frozenset[str]:
    """What *runtime* loads: the image's answer when there is one, else ours."""
    key = runtime.strip().lower()
    found = _PROBED.get(key)
    if found is not None:
        return found.architectures
    profile = RUNTIMES.get(key)
    return profile.architectures if profile is not None else frozenset()


def normalize_architecture(name: str) -> str:
    """Accept either a transformers class name or a ggml architecture name."""
    return GGUF_ARCHITECTURE_NAMES.get(name.strip().lower(), name)


def runtimes_serving(architectures) -> list[str]:
    """Every runtime in this build that actually loads one of these
    architectures -- registered *and* not known to crash there.

    Registration alone is not enough: `DiffusionGemmaForBlockDiffusion` is in
    vLLM's own registry and still refused, via `VLLM_KNOWN_BROKEN`, because it
    dies in CUDA graph capture. Pointing a reader refused on sglang or tts at
    "the vllm runtime loads it" would be false in exactly the case where the
    reason matters most -- the runtime that "loads" it also refuses it, for a
    documented crash rather than a missing name.
    """
    return [
        name
        for name in RUNTIMES
        if any(
            a in architectures_for(name) and a not in RUNTIMES[name].known_broken
            for a in architectures or ()
        )
    ]


def _elsewhere(architectures: tuple[str, ...], excluding: str) -> str:
    """`; the tts runtime loads it, on /v1/audio/speech`, or nothing.

    A refusal names what to change. "Not in vllm's list" is true and leaves
    the reader with no next move; the model in front of them may be perfectly
    servable one control away, and this is the only place that knows it.
    """
    others = [name for name in runtimes_serving(architectures) if name != excluding]
    if not others:
        return ""
    endpoint = ENDPOINT_FOR_MODALITY.get(Modality(modality_for(architectures)))
    where = f", on {endpoint}" if endpoint and endpoint != "/v1/chat/completions" else ""
    return f"; the {others[0]} runtime loads it{where}"


def quant_requirement(dtype: str) -> QuantRequirement:
    info = quant_info(dtype)
    return QuantRequirement(
        dtype=info.key,
        native_compute_capability=info.native_compute_capability,
        emulated_below_native=info.emulated_below_native,
        note=info.note,
    )


def _quant_hint(runtime: str, dtype: str) -> str:
    """The half of a quantization refusal that says where to go instead.

    Two directions, and they are not symmetric. Telling somebody that GGUF
    "belongs on a llama.cpp backend" was, for the life of that sentence, a
    statement about a backend this project did not have; now it names one, and
    the sentence stops being a shrug. The reverse hint is new and matters
    more, because it is the one an operator hits by accident: llama.cpp is the
    runtime you pick for a machine with no GPU, and then you point it at the
    ordinary safetensors repository you already had open.

    Silence for everything else. A note that does not name an alternative is
    noise on the end of a refusal that was already clear.
    """
    family = quant_info(dtype).family
    if runtime == "llamacpp":
        if family != "gguf":
            return (
                "; llama.cpp reads GGUF and nothing else, so this needs a GGUF "
                "build of the same model rather than the original weights"
            )
        return ""
    if family == "gguf":
        return "; GGUF is llama.cpp's format and belongs on a llama.cpp backend"
    return ""


def evaluate_runtime(
    runtime: str, architectures: tuple[str, ...], dtype: str
) -> RuntimeSupport:
    """Architecture and quantization support for one runtime."""
    key = runtime.strip().lower()
    profile = RUNTIMES.get(key)
    if profile is None:
        return RuntimeSupport(key, SupportLevel.UNVERIFIED, f"unknown runtime {runtime!r}")

    architectures = tuple(normalize_architecture(a) for a in architectures)
    # The image's own registry when it has been read, the static table when
    # it has not. `found` is also what decides how a refusal is worded below:
    # "not in our list" and "not in the registry of the image we launch" are
    # different strengths of claim and the operator has to be able to tell
    # them apart.
    found = _PROBED.get(key)
    known_architectures = found.architectures if found is not None else profile.architectures
    #: The image's own version string, when a probe has run. Carried on every
    #: verdict below, not only the ones whose reason happens to mention it, so
    #: the UI can show "checked against 0.28.1rc1.dev486" next to a model that
    #: was never refused a word about which build okayed it.
    version = found.version if found is not None else None
    quant_level = profile.quants.get(dtype, SupportLevel.UNSUPPORTED)
    if quant_level is SupportLevel.UNSUPPORTED:
        return RuntimeSupport(
            key,
            SupportLevel.UNSUPPORTED,
            f"{profile.name} cannot load {dtype} weights"
            + _quant_hint(key, dtype),
            version,
        )

    known = [a for a in architectures if a in known_architectures]
    broken = profile.known_broken.get(known[0]) if known else None
    if not architectures:
        arch_level, arch_reason = (
            SupportLevel.UNVERIFIED,
            "no architecture on record for this shape, so runtime support is "
            "unverified; resolve the model id first and the check becomes exact",
        )
    elif broken is not None:
        arch_level, arch_reason = (
            SupportLevel.UNSUPPORTED,
            f"{profile.name} lists {known[0]} in its registry but it does "
            f"not actually run: {broken}",
        )
    elif known:
        arch_level, arch_reason = (
            SupportLevel.SUPPORTED,
            f"{profile.name} supports {known[0]}",
        )
    elif found is not None and architectures[0] in found.out_of_tree:
        # vLLM moved this one out of tree; refusing without naming the plugin
        # would be true and leave the reader nowhere to go, same as every
        # other refusal here.
        arch_level, arch_reason = (
            SupportLevel.UNSUPPORTED,
            f"{architectures[0]} is loaded by a plugin, not built into "
            f"{found.provenance}: {found.out_of_tree[architectures[0]]}"
            f"{_elsewhere(architectures, key)}",
        )
    elif found is not None and architectures[0] in found.removed:
        # vLLM shipped this once and dropped it. Distinct from "never
        # supported" for the same reason DiffusionGemma's broken-not-missing
        # case is distinct above: the fix is different (an older image) and
        # the operator can't tell the two apart from "is not in the list".
        arch_level, arch_reason = (
            SupportLevel.UNSUPPORTED,
            f"{architectures[0]} was supported by vLLM through "
            f"{found.removed[architectures[0]]} and is not in {found.provenance}'s "
            f"registry -- not available in a newer image, only an older one"
            f"{_elsewhere(architectures, key)}",
        )
    else:
        where = (
            f"the model registry of {found.provenance}, the image this build "
            f"launches"
            if found is not None
            else f"{profile.name}'s supported architecture list"
        )
        arch_level, arch_reason = (
            SupportLevel.UNSUPPORTED,
            f"{architectures[0]} is not in {where}{_elsewhere(architectures, key)}",
        )

    if arch_level is SupportLevel.UNSUPPORTED:
        return RuntimeSupport(key, SupportLevel.UNSUPPORTED, arch_reason, version)
    if quant_level is SupportLevel.UNVERIFIED:
        return RuntimeSupport(
            key,
            SupportLevel.UNVERIFIED,
            f"{arch_reason}, but {profile.name}'s {dtype} path is experimental",
            version,
        )
    if arch_level is SupportLevel.UNVERIFIED:
        return RuntimeSupport(key, SupportLevel.UNVERIFIED, arch_reason, version)
    return RuntimeSupport(key, SupportLevel.SUPPORTED, f"{arch_reason} at {dtype}", version)


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
            problems.append(f"{node.describe()}: {reason}")
        elif "emulated" in reason:
            problems.append(f"{node.describe()}: {reason}")
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
