"""Ask the runtime image what it can load, instead of keeping a list.

The support table in ``support.py`` is a hand-copied claim about somebody
else's software, and it goes stale in the one direction nobody notices: it
keeps refusing models that started working. ``Gemma4ForConditionalGeneration``
was refused with "not in vllm's supported architecture list" while the pinned
image had been able to load it all along, and the same morning the same list
was refusing DeepSeek-V4, Qwen3-Next, Qwen3.5/3.6/3.8, LFM2.5 and
llava-onevision. Every one of those is a launch the operator was told was
impossible.

vLLM and SGLang both know the answer exactly -- each keeps a model registry as
a dict in the image -- so this module asks each and caches what it says. The
static set stays as the fallback for a coordinator with no docker, no image,
or a runtime with no script here at all (``tts``), which is a real deployment:
nothing here ever fails a startup or a resolve.

Three rules, each of which a simpler version of this got wrong:

**Never pull.** ``docker image inspect`` runs first and a miss is ``None``.
The image is 24 GB; pulling it inside a resolve would turn a page load into a
half-hour download on a machine that may not even be the one serving models.

**Cache by image id, not by tag.** The pinned tag is
``dgx-vllm-eugr-nightly:latest`` and it moves. A cache keyed by the tag would
reintroduce exactly the staleness this exists to end -- the entry would still
be answering for the image that tag pointed at last week. Keyed by the id, a
moved tag is a cache miss and re-probes itself.

**Degrade, never raise.** No docker, no image, a timeout, a non-zero exit, an
unparseable answer: all of them are ``None`` and the caller keeps its static
table. This is the same contract as the fit gate's live-memory kwarg -- the
better number is used when it is there and its absence is never a refusal.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: Read vLLM's own model registry and print the servable set.
#:
#: The subtractions are the claims the raw registry would otherwise make
#: wrongly. Draft heads go -- ``Gemma4MTPModel`` is a speculator a real model
#: loads, not a server -- but they are *kept* rather than discarded, under
#: their own key: that set is the only evidence this project has for which
#: speculative methods the pinned image can actually load. A method the
#: checkpoint declares and the image cannot load is a launch that clears every
#: gate here and dies at load, which is the same failure the architecture list
#: exists to prevent. The transformers-backend wrappers go --
#: ``TransformersForCausalLM`` is an implementation vLLM picks, never a name
#: in a checkpoint's ``config.json``. Pooling-only models go -- embedding,
#: reward, classification -- but only those with no generative path, because
#: vLLM registers ``GlmForCausalLM`` and ``DeciLMForCausalLM`` in both places
#: and they chat.
#:
#: Anything vLLM has dropped is absent by construction, which matters as much
#: as the additions: ``MllamaForConditionalGeneration`` (Llama 3.2 Vision) is
#: in the static table and this image cannot load it.
#:
#: Two more dicts ride along for free: ``_PREVIOUSLY_SUPPORTED_MODELS`` (name
#: -> the last vLLM version that had it) and ``_OOT_SUPPORTED_MODELS`` (name ->
#: a plugin URL vLLM now points at instead). Neither costs anything beyond the
#: import already paid for the sets above -- no per-class inspection, no
#: subprocess -- and they are what let a refusal say *why* an architecture is
#: missing instead of just that it is: dropped in v0.10.2, or moved to
#: https://github.com/vllm-project/bart-plugin, rather than one dead-end
#: sentence for every miss.
_VLLM_SCRIPT = """
import json
import vllm
import vllm.model_executor.models.registry as r

cat = lambda n: set(getattr(r, n, ()))
pooling = (cat("_EMBEDDING_MODELS") | cat("_REWARD_MODELS")
           | cat("_SEQUENCE_CLASSIFICATION_MODELS")
           | cat("_TOKEN_CLASSIFICATION_MODELS")
           | cat("_LATE_INTERACTION_MODELS"))
generative = cat("_TEXT_GENERATION_MODELS") | cat("_MULTIMODAL_MODELS")
speculators = cat("_SPECULATIVE_DECODING_MODELS")
servable = (set(r._VLLM_MODELS) - speculators
            - cat("_TRANSFORMERS_BACKEND_MODELS") - (pooling - generative))
print("derate-imageprobe:" + json.dumps({
    "version": vllm.__version__,
    "architectures": sorted(servable),
    "speculators": sorted(speculators),
    "removed": dict(getattr(r, "_PREVIOUSLY_SUPPORTED_MODELS", {})),
    "out_of_tree": dict(getattr(r, "_OOT_SUPPORTED_MODELS", {})),
}))
"""

#: SGLang's registry (`sglang.srt.models.registry.ModelRegistry.models`) is a
#: single flat dict, architecture name -> class, with none of vLLM's
#: `_EMBEDDING_MODELS` / `_SPECULATIVE_DECODING_MODELS` / etc. category sets to
#: subtract by. What "servable" means has to come from the class itself.
#:
#: The class's own name is not enough -- verified against a real image
#: (`scitrera/dgx-spark-sglang:0.5.12`) rather than assumed: `MistralModel` is
#: pooling-only (`class MistralModel(LlamaEmbeddingModel): pass`, in
#: `llama_embedding.py`) and its own name says nothing about that. So this
#: checks every name in the class's MRO, not just the leaf class -- an
#: embedding subclass inherits its ancestor's tell even when it drops the word
#: from its own name. One mixin present on that same image is a false
#: friend: `EmbeddingAccessMixin` sits on plainly generative classes too
#: (`Gemma3ForCausalLM`, `Gemma4ForConditionalGeneration`, ...) -- it is a
#: get/set-embeddings utility, not a pooling marker, and is ignored on purpose.
#:
#: Draft heads (Eagle/MTP/NextN/DraftModel in the name) and the four generic
#: `Transformers*` wrappers are excluded for the same reasons `_VLLM_SCRIPT`
#: excludes their vLLM equivalents: a draft head is never a checkpoint's own
#: declared architecture, and a backend wrapper is an implementation vLLM/SGLang
#: picks, not a name in `config.json`.
#:
#: This is a heuristic, not an exact read the way vLLM's category sets are --
#: SGLang exposes no equivalent of `_ModelInfo.is_text_generation_model` to ask
#: directly. A wrong classification here means the same bounded risk the static
#: tables already carry: a name added or kept on the strength of a guess,
#: correctable the same way -- watch a launch, fix the entry.
_SGLANG_SCRIPT = """
import json
import sglang
from sglang.srt.models.registry import ModelRegistry

SPECULATIVE_MARKERS = ("Eagle", "MTP", "NextN", "DraftModel")
POOLING_MARKERS = ("EmbeddingModel", "EmbeddingMixin", "ForRewardModel",
                    "ForSequenceClassification", "ForClassification", "PooledOutput")
BARE_BACKBONE_MARKERS = ("BertModel", "XLMRobertaModel", "CLIPModel", "Contriever", "VisionModel")
TRANSFORMERS_WRAPPERS = {
    "TransformersForCausalLM", "TransformersMoEForCausalLM",
    "TransformersMultiModalForCausalLM", "TransformersMultiModalMoEForCausalLM",
}
IGNORE_ANCESTORS = {"EmbeddingAccessMixin"}

def ancestor_names(cls):
    return {b.__name__ for b in cls.__mro__} - IGNORE_ANCESTORS

def is_servable(name, cls):
    if name in TRANSFORMERS_WRAPPERS:
        return False
    names = ancestor_names(cls) | {name}
    markers = SPECULATIVE_MARKERS + POOLING_MARKERS + BARE_BACKBONE_MARKERS
    return not any(marker in n for n in names for marker in markers)

servable = sorted(
    name for name, cls in ModelRegistry.models.items()
    if isinstance(cls, type) and is_servable(name, cls)
)
print("derate-imageprobe:" + json.dumps({
    "version": sglang.__version__,
    "architectures": servable,
}))
"""

#: Per-runtime introspection. `tts` has no script: it is derate's own server
#: and its support table is authored, not read out of somebody else's image.
SCRIPTS: dict[str, str] = {"vllm": _VLLM_SCRIPT, "sglang": _SGLANG_SCRIPT}

#: The marker the script prints its JSON behind.
#:
#: It has to be findable in the middle of noise rather than assumed to be the
#: whole of stdout: vLLM writes INFO lines about Triton and CUDA on import,
#: before anything here gets to speak.
#:
#: Lower-case and hyphenated on purpose. Spelled in the shouting style this
#: project uses for environment variables, it was picked up by
#: `test_single_source`'s scan of the tree and reported as a variable nobody
#: had declared in `envspec.py`. It is a line prefix, not a variable, and it
#: should not read like one.
_MARKER = "derate-imageprobe:"


@dataclass(frozen=True)
class ImageProbe:
    """What one runtime image says it can load."""

    runtime: str
    image: str
    image_id: str
    version: str
    architectures: frozenset[str]
    #: The speculator classes this image registers -- ``Gemma4MTPModel``,
    #: ``Gemma4DSparkModel`` and their siblings. Not servable models: they are
    #: draft heads a real model loads, and their presence is what makes
    #: offering a speculative method evidence rather than a claim.
    speculators: frozenset[str] = frozenset()
    #: Architecture -> the last vLLM version that had it, straight out of
    #: vLLM's own ``_PREVIOUSLY_SUPPORTED_MODELS``. Lets a refusal say "dropped
    #: in v0.10.2" instead of the same dead-end sentence as a name that was
    #: never supported at all.
    removed: dict[str, str] = field(default_factory=dict)
    #: Architecture -> a plugin URL vLLM points at instead, straight out of
    #: ``_OOT_SUPPORTED_MODELS``. Lets a refusal name the plugin rather than
    #: just say the architecture is missing.
    out_of_tree: dict[str, str] = field(default_factory=dict)

    @property
    def provenance(self) -> str:
        """`ghcr.io/...:latest (vLLM 0.28.1rc1.dev462)`, for a refusal to cite.

        A refusal names what to change, and "not in vllm's list" leaves the
        reader with nowhere to go. Naming the image and its version turns the
        same sentence into something checkable: pull a newer image, or read
        the model card again.
        """
        return f"{self.image} ({self.version})"

    def as_dict(self) -> dict:
        return {
            "runtime": self.runtime,
            "image": self.image,
            "image_id": self.image_id,
            "version": self.version,
            "architectures": sorted(self.architectures),
            "speculators": sorted(self.speculators),
            "removed": dict(self.removed),
            "out_of_tree": dict(self.out_of_tree),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImageProbe":
        # `speculators`, `removed` and `out_of_tree` are required, not
        # defaulted, and that is the point. The cache is keyed by image id, so
        # an entry written before one of these fields existed would otherwise
        # be served forever as "this image registers no speculators" / "vLLM
        # never dropped or moved anything" -- false for every vLLM build there
        # has ever been, and would silently withdraw evidence on a box that had
        # probed once already. A KeyError here is a cache miss in
        # `_read_cache`, which re-probes; that is the correct outcome.
        return cls(
            runtime=data["runtime"],
            image=data["image"],
            image_id=data["image_id"],
            version=data["version"],
            architectures=frozenset(data["architectures"]),
            speculators=frozenset(data["speculators"]),
            removed=dict(data["removed"]),
            out_of_tree=dict(data["out_of_tree"]),
        )


def _run(args: list[str], timeout: float) -> str | None:
    """Stdout of *args*, or None for anything that went wrong.

    Every failure mode of shelling out to docker collapses here: the binary
    is not installed, the daemon is not running, the user is not in the
    group, the image is absent, the container died. None of them is worth
    distinguishing at the call site -- they all mean "no better answer than
    the static table" -- and none of them may propagate.
    """
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout


def image_id(image: str, *, docker: str = "docker", timeout: float = 30.0) -> str | None:
    """The local image's content id, or None when it is not on this machine.

    This is also the "do we probe at all" gate. It never pulls: ``inspect``
    fails on a missing image rather than fetching it, which is the whole
    point on a coordinator that may not be the machine that serves models.
    """
    out = _run([docker, "image", "inspect", "--format", "{{.Id}}", image], timeout)
    if out is None:
        return None
    line = out.strip().splitlines()[0].strip() if out.strip() else ""
    return line or None


def _cache_path(cache_dir: Path, runtime: str, ident: str) -> Path:
    return cache_dir / f"{runtime}-{ident.replace(':', '-')}.json"


def probe(
    runtime: str,
    image: str,
    *,
    cache_dir: Path | None = None,
    docker: str = "docker",
    timeout: float = 300.0,
) -> ImageProbe | None:
    """What *image* can load, from the image itself. None when it cannot be asked.

    Cached under *cache_dir* by image id, so this costs one container start
    the first time a given image is seen and nothing afterwards. A moved tag
    changes the id and re-probes; that is the whole reason the id is the key.
    """
    script = SCRIPTS.get(runtime)
    if script is None:
        return None

    ident = image_id(image, docker=docker, timeout=min(timeout, 30.0))
    if ident is None:
        return None

    if cache_dir is not None:
        hit = _read_cache(_cache_path(cache_dir, runtime, ident))
        if hit is not None:
            return hit

    out = _run(
        [docker, "run", "--rm", "--entrypoint", "python3", image, "-c", script],
        timeout,
    )
    if out is None:
        return None

    payload = None
    for line in out.splitlines():
        if line.startswith(_MARKER):
            try:
                payload = json.loads(line[len(_MARKER):])
            except ValueError:
                return None
            break
    if not payload or not payload.get("architectures"):
        return None

    found = ImageProbe(
        runtime=runtime,
        image=image,
        image_id=ident,
        version=str(payload.get("version") or "unknown"),
        architectures=frozenset(payload["architectures"]),
        # Not part of the "did the script answer at all" guard above, unlike
        # `architectures`: an image that registers no speculator classes is a
        # coherent answer, and treating it as a failed probe would throw away a
        # perfectly good architecture list with it. Same for `removed` and
        # `out_of_tree` being empty -- a future vLLM that has dropped nothing
        # is a coherent answer too.
        speculators=frozenset(payload.get("speculators") or ()),
        removed=dict(payload.get("removed") or {}),
        out_of_tree=dict(payload.get("out_of_tree") or {}),
    )
    if cache_dir is not None:
        _write_cache(_cache_path(cache_dir, runtime, ident), found)
    return found


def _read_cache(path: Path) -> ImageProbe | None:
    try:
        return ImageProbe.from_dict(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError):
        return None


def _write_cache(path: Path, found: ImageProbe) -> None:
    """Best effort. An unwritable cache costs a container start, not a probe."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(found.as_dict(), indent=2))
        os.replace(tmp, path)
    except OSError:
        pass
