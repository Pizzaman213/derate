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

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
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
#:
#: Two silicon facts ride along, both from vLLM's own code rather than from a
#: table here, because a GPU generation the image cannot serve is a launch that
#: clears every other gate and dies at load:
#:
#: ``quant_min_capability`` is each quantization method's
#: ``get_min_capability()`` (75 for ``fp8``, 100 for ``fp_quant``) -- the floor
#: vLLM itself refuses below. A classmethod: nothing is allocated.
#:
#: ``kv_capabilities`` is, per KV cache dtype, the compute capabilities at which
#: vLLM's own attention selector (``CudaPlatform.get_valid_backends``) finds a
#: backend for an ordinary (non-MLA) layer at head size 128, 64 or 256. Measured
#: on 0.28.1 and 0.31.0: the plain widths and fp8 are valid everywhere from
#: sm_75 to sm_121, and ``nvfp4`` is valid NOWHERE -- FlashInfer itself answers
#: "kv_cache_dtype not supported" at sm_100 and sm_121. ``auto`` is valid at
#: every capability that was asked, so its list doubles as the set asked: a
#: capability absent from it is one nobody has evidence about. Importing
#: ``vllm.platforms.cuda`` needs libcuda, which is why the probe runs with
#: ``--gpus all`` (a driver mount; nothing is allocated) -- and the field is
#: null when it could not be imported, which means "unknown", never "none".
#: The MLA variant of the same question raised on both images, so MLA layers
#: are not asked at all: no evidence is no refusal.
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
from vllm.plugins import load_general_plugins
load_general_plugins()
from vllm.model_executor.layers.quantization import (
    QUANTIZATION_METHODS, get_quantization_config)

qmin = {}
for m in sorted(QUANTIZATION_METHODS):
    try:
        qmin[m] = int(get_quantization_config(m).get_min_capability())
    except Exception:
        pass

kv = None
try:
    import typing
    import torch
    from vllm.platforms.cuda import CudaPlatform
    from vllm.platforms.interface import DeviceCapability
    from vllm.v1.attention.selector import AttentionSelectorConfig
    try:
        from vllm.config.cache import CacheDType
        dtypes = typing.get_args(CacheDType)
    except ImportError:
        from vllm.config import CacheConfig
        dtypes = typing.get_args(CacheConfig.__annotations__["cache_dtype"])
    caps = [(7, 5), (8, 0), (8, 6), (8, 7), (8, 9), (9, 0), (10, 0), (10, 3),
            (11, 0), (12, 0), (12, 1)]
    defaults = AttentionSelectorConfig._field_defaults
    kv = {}
    for d in dtypes:
        ok = []
        for c in caps:
            for hs in (128, 64, 256):
                kw = dict(head_size=hs, dtype=torch.bfloat16, kv_cache_dtype=d,
                          block_size=None, use_mla=False)
                for f in AttentionSelectorConfig._fields:
                    kw.setdefault(f, defaults.get(f, False))
                try:
                    valid, _ = CudaPlatform.get_valid_backends(
                        DeviceCapability(*c), AttentionSelectorConfig(**kw))
                except Exception:
                    valid = []
                if valid:
                    ok.append("%d.%d" % c)
                    break
        kv[d] = ok
except Exception:
    kv = None

print("derate-imageprobe:" + json.dumps({
    "version": vllm.__version__,
    "architectures": sorted(servable),
    "speculators": sorted(speculators),
    "removed": dict(getattr(r, "_PREVIOUSLY_SUPPORTED_MODELS", {})),
    "out_of_tree": dict(getattr(r, "_OOT_SUPPORTED_MODELS", {})),
    "gguf": "gguf" in QUANTIZATION_METHODS,
    "quant_min_capability": qmin,
    "kv_capabilities": kv,
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
    #: vLLM's GGUF loader is a plugin since 0.27; whether this image carries
    #: it. Read off ``QUANTIZATION_METHODS`` after vLLM's own plugin loader
    #: has run, which is exactly what a launch does. Always False for an
    #: SGLang probe, whose script does not ask.
    gguf: bool = False
    #: Quantization method -> the lowest compute capability vLLM loads it at,
    #: as vLLM spells both (``"fp8": 75``, ``"fp_quant": 100``). Read off each
    #: method's own ``get_min_capability()``, which is the check vLLM itself
    #: raises on at load. Empty for an SGLang probe, whose script does not ask.
    quant_min_capability: dict[str, int] = field(default_factory=dict)
    #: vLLM KV cache dtype -> the capabilities (``"8.6"``) at which the image's
    #: own attention selector finds a backend for a non-MLA layer. None when
    #: the probe could not import the CUDA platform (no GPU driver mounted),
    #: which is "unknown" and must never be read as "valid nowhere". The list
    #: for ``auto`` is every capability that was asked.
    kv_capabilities: dict[str, tuple[str, ...]] | None = None

    def kv_asked(self) -> frozenset[str]:
        """Every capability the KV selector was asked about. A capability
        outside it is one this probe has no evidence for, either way."""
        if not self.kv_capabilities:
            return frozenset()
        return frozenset(c for caps in self.kv_capabilities.values() for c in caps)

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
            "gguf": self.gguf,
            "quant_min_capability": dict(self.quant_min_capability),
            "kv_capabilities": (
                None
                if self.kv_capabilities is None
                else {k: list(v) for k, v in self.kv_capabilities.items()}
            ),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImageProbe":
        # `speculators`, `removed`, `out_of_tree`, `gguf`,
        # `quant_min_capability` and `kv_capabilities` are required, not
        # defaulted, and that is the point. The cache is keyed by image id, so
        # an entry written before one of these fields existed would otherwise
        # be served forever as "this image registers no speculators" / "vLLM
        # never dropped or moved anything" / "this image has no GGUF loader"
        # / "nothing is known about this image's silicon floors" -- false for
        # every vLLM build there has ever been (or, for `gguf`, for any image
        # that carries the plugin), and would silently withdraw evidence on a
        # box that had probed once already. A KeyError here is a cache miss
        # in `_read_cache`, which re-probes; that is the correct outcome.
        return cls(
            runtime=data["runtime"],
            image=data["image"],
            image_id=data["image_id"],
            version=data["version"],
            architectures=frozenset(data["architectures"]),
            speculators=frozenset(data["speculators"]),
            removed=dict(data["removed"]),
            out_of_tree=dict(data["out_of_tree"]),
            gguf=bool(data["gguf"]),
            quant_min_capability=_min_capabilities(data["quant_min_capability"]),
            kv_capabilities=_kv_capabilities(data["kv_capabilities"]),
        )


def _min_capabilities(raw) -> dict[str, int]:
    """`{method: int}`, dropping anything that is not one."""
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): int(v)
        for k, v in raw.items()
        if isinstance(v, int) and not isinstance(v, bool)
    }


def _kv_capabilities(raw) -> dict[str, tuple[str, ...]] | None:
    """`{kv dtype: ("8.6", ...)}`, or None for "the probe could not ask"."""
    if not isinstance(raw, dict):
        return None
    return {
        str(k): tuple(str(c) for c in v)
        for k, v in raw.items()
        if isinstance(v, (list, tuple))
    }


def _run(args: list[str], timeout: float) -> str | None:
    """Stdout of *args*, or None for anything that went wrong.

    Every failure mode of shelling out to docker collapses here: the binary
    is not installed, the daemon is not running, the user is not in the
    group, the image is absent, the container died. None of them is worth
    distinguishing at the call site -- they all mean "no better answer than
    the static table" -- and none of them may propagate.
    """
    done = _exec(args, timeout)
    if done is None or done.returncode != 0:
        return None
    return done.stdout


def _exec(args: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """The finished process, or None when it never ran or ran out of time."""
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _docker(docker: str, host: str | None) -> list[str]:
    """The docker argv prefix: this machine's daemon, or *host*'s over SSH."""
    return [docker, "-H", host] if host else [docker]


def image_id(
    image: str, *, docker: str = "docker", timeout: float = 30.0, host: str | None = None
) -> str | None:
    """The image's content id, or None when it is not on the machine asked.

    This is also the "do we probe at all" gate. It never pulls: ``inspect``
    fails on a missing image rather than fetching it, which is the whole
    point on a coordinator that may not be the machine that serves models.
    """
    out = _run([*_docker(docker, host), "image", "inspect", "--format", "{{.Id}}", image], timeout)
    if out is None:
        return None
    line = out.strip().splitlines()[0].strip() if out.strip() else ""
    return line or None


def _identity(
    image: str, *, docker: str, timeout: float, host: str | None
) -> tuple[str, str] | None:
    """``(docker id, content key)`` of the image on the machine asked, or None.

    The content key is what the probe cache is keyed by, and it is NOT the
    docker id: that id depends on the daemon's image store. The same image is
    ``sha256:db08...`` on the RTX box (containerd store, where the id is the
    manifest digest) and ``sha256:1d61...`` on the coordinator after `docker
    save | docker load` (overlay2, where it is the config digest). Keyed by the
    id, a probe run on one amd64 machine could never answer for the next one.
    The ordered layer diff ids are identical in both stores, so their digest
    is the key; the id stays as the fallback for an image that reports none.

    Both come from a daemon that may be another machine's, so neither is
    trusted as a string: the id and every layer must be a ``sha256:`` digest
    or there is no identity and no probe. The key is then built only from
    those validated hex digits, because it becomes a file name.
    """
    out = _run(
        [*_docker(docker, host), "image", "inspect", "--format",
         "{{.Id}}|{{json .RootFS.Layers}}", image],
        timeout,
    )
    if not out or not out.strip():
        return None
    ident, _, layers_json = out.strip().splitlines()[0].partition("|")
    ident = ident.strip()
    if not _DIGEST.fullmatch(ident):
        return None
    try:
        layers = json.loads(layers_json) if layers_json else None
    except ValueError:
        return None
    if not layers:
        return ident, ident.replace(":", "-")
    if not isinstance(layers, list) or not all(
        isinstance(layer, str) and _DIGEST.fullmatch(layer) for layer in layers
    ):
        return None
    digest = hashlib.sha256("\n".join(layers).encode()).hexdigest()
    return ident, "layers-" + digest


#: A docker content digest, the only shape an image id or a layer may have.
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
#: The only shapes `_identity` produces as a cache key.
_KEY = re.compile(r"(?:sha256|layers)-[0-9a-f]{64}")


def _cache_path(
    cache_dir: Path, runtime: str, key: str, host: str | None = None
) -> Path | None:
    """Where the probe of *key* is cached, or None when that would not be a
    plain file directly inside *cache_dir*.

    With *host*, the entry is that host's alone: an answer from another
    machine's daemon that the coordinator could not confirm against its own
    copy. The host is hashed rather than spelled, so no daemon- or
    roster-supplied text is ever part of a path.
    """
    if not _KEY.fullmatch(key) or not runtime.isidentifier():
        return None
    scope = ""
    if host is not None:
        scope = "host-" + hashlib.sha256(host.encode()).hexdigest()[:16] + "-"
    path = cache_dir / f"{runtime}-{scope}{key}.json"
    try:
        if path.resolve().parent != cache_dir.resolve():
            return None
    except OSError:
        return None
    return path


def probe(
    runtime: str,
    image: str,
    *,
    cache_dir: Path | None = None,
    docker: str = "docker",
    timeout: float = 300.0,
    host: str | None = None,
    run: bool = True,
) -> ImageProbe | None:
    """What *image* can load, from the image itself. None when it cannot be asked.

    Cached under *cache_dir* by the image's layer digests (``_identity``), so
    this costs one container start the first time a given image is seen,
    anywhere, and nothing afterwards. A moved tag changes the layers and
    re-probes; that is the whole reason content is the key.

    *host* (``ssh://10.0.0.5``) runs it on another machine's daemon instead,
    which is the only way to ask an image built for an architecture the
    coordinator is not: an amd64 image cannot start on an arm64 coordinator.
    That answer comes from somebody else's daemon, so it becomes the SHARED
    content-keyed entry -- the one every machine of that architecture, and the
    ``run=False`` lookup, is judged by -- only when the coordinator holds its
    own copy of the image with the same layer key. Otherwise it is cached for
    that host alone, and one compromised node cannot define what an image
    loads for the rest of the cluster. Each entry records where it came from.

    ``run=False`` answers from the cache only. That is how the coordinator
    asks about an image it holds for ANOTHER architecture (so sparkrun can
    ship it): it can inspect it and look its layers up, but starting it would
    only fail with ``exec format error``.
    """
    script = SCRIPTS.get(runtime)
    if script is None:
        return None

    inspect_timeout = min(timeout, 30.0)
    identity = _identity(image, docker=docker, timeout=inspect_timeout, host=host)
    if identity is None:
        return None
    ident, key = identity

    shared = scoped = None
    if cache_dir is not None:
        shared = _cache_path(cache_dir, runtime, key)
        scoped = _cache_path(cache_dir, runtime, key, host) if host is not None else None
        if shared is None or (host is not None and scoped is None):
            return None
        for path in (shared, scoped):
            hit = _read_cache(path) if path is not None else None
            if hit is not None:
                # Content-keyed, so the same layers may have been probed under
                # another tag or on another daemon; answer for the one asked.
                return replace(hit, image=image, image_id=ident)
    if not run:
        return None

    # `--gpus all` first: the KV-selector half of the vllm script imports
    # vllm's CUDA platform, which needs the driver library mounted (nothing is
    # allocated). A daemon with no NVIDIA runtime refuses that flag outright,
    # and a GPU-less coordinator must still get the architecture list, so a
    # refusal is retried without it -- the payload's `kv_capabilities` is then
    # null, which reads as "unknown". A timeout is not retried: the same
    # container would time out again.
    base = [*_docker(docker, host), "run", "--rm"]
    tail = ["--entrypoint", "python3", image, "-c", script]
    done = _exec([*base, "--gpus", "all", *tail], timeout)
    if done is None:
        return None
    if done.returncode != 0:
        done = _exec([*base, *tail], timeout)
        if done is None or done.returncode != 0:
            return None
    out = done.stdout

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
        gguf=bool(payload.get("gguf")),
        quant_min_capability=_min_capabilities(payload.get("quant_min_capability")),
        kv_capabilities=_kv_capabilities(payload.get("kv_capabilities")),
    )
    if shared is not None:
        if host is None:
            _write_cache(shared, found, source="local")
        else:
            local = _identity(image, docker=docker, timeout=inspect_timeout, host=None)
            if local is not None and local[1] == key:
                _write_cache(shared, found, source=f"{host}, confirmed by the coordinator's copy")
            elif scoped is not None:
                _write_cache(scoped, found, source=f"{host}, unconfirmed: that host only")
    return found


def _read_cache(path: Path) -> ImageProbe | None:
    try:
        return ImageProbe.from_dict(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError):
        return None


def _write_cache(path: Path, found: ImageProbe, *, source: str) -> None:
    """Best effort. An unwritable cache costs a container start, not a probe.

    *source* says which daemon answered and whether it may speak for others;
    `from_dict` ignores it, so it is for whoever reads the file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**found.as_dict(), "source": source}, indent=2))
        os.replace(tmp, path)
    except OSError:
        pass
