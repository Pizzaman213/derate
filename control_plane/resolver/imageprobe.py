"""Ask the runtime image what it can load, instead of keeping a list.

The support table in ``support.py`` is a hand-copied claim about somebody
else's software, and it goes stale in the one direction nobody notices: it
keeps refusing models that started working. ``Gemma4ForConditionalGeneration``
was refused with "not in vllm's supported architecture list" while the pinned
image had been able to load it all along, and the same morning the same list
was refusing DeepSeek-V4, Qwen3-Next, Qwen3.5/3.6/3.8, LFM2.5 and
llava-onevision. Every one of those is a launch the operator was told was
impossible.

vLLM knows the answer exactly -- its model registry is a dict in the image --
so this module asks it and caches what it says. The static set stays as the
fallback for a coordinator with no docker and no image, which is a real
deployment: nothing here ever fails a startup or a resolve.

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
from dataclasses import dataclass
from pathlib import Path

#: Read vLLM's own model registry and print the servable set.
#:
#: The subtractions are the claims the raw registry would otherwise make
#: wrongly. Draft heads go -- ``Gemma4MTPModel`` is a speculator a real model
#: loads, not a server. The transformers-backend wrappers go --
#: ``TransformersForCausalLM`` is an implementation vLLM picks, never a name
#: in a checkpoint's ``config.json``. Pooling-only models go -- embedding,
#: reward, classification -- but only those with no generative path, because
#: vLLM registers ``GlmForCausalLM`` and ``DeciLMForCausalLM`` in both places
#: and they chat.
#:
#: Anything vLLM has dropped is absent by construction, which matters as much
#: as the additions: ``MllamaForConditionalGeneration`` (Llama 3.2 Vision) is
#: in the static table and this image cannot load it.
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
servable = (set(r._VLLM_MODELS) - cat("_SPECULATIVE_DECODING_MODELS")
            - cat("_TRANSFORMERS_BACKEND_MODELS") - (pooling - generative))
print("derate-imageprobe:" + json.dumps({
    "version": vllm.__version__,
    "architectures": sorted(servable),
}))
"""

#: Per-runtime introspection. Only vllm for now, and deliberately: SGLang's
#: registry is a different shape and the pinned SGLang image is not on the box
#: this was written on, so a script for it would be guesswork wearing the
#: costume of evidence. A runtime with no entry here simply keeps its static
#: table.
SCRIPTS: dict[str, str] = {"vllm": _VLLM_SCRIPT}

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
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImageProbe":
        return cls(
            runtime=data["runtime"],
            image=data["image"],
            image_id=data["image_id"],
            version=data["version"],
            architectures=frozenset(data["architectures"]),
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
