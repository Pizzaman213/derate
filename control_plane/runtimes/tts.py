"""The text-to-speech server: text in, audio bytes out, on /v1/audio/speech.

Why this exists at all. The gateway has proxied ``POST /v1/audio/speech``
since audio landed, but nothing in the cluster could answer it: vLLM serves
transcription and not synthesis, SGLang serves neither, and a TTS checkpoint's
architecture is in neither project's supported list, so the launch was refused
before the fit gate was even consulted. README's "three ways to serve audio"
made local TTS somebody else's container. This is the fourth: a runtime derate
launches itself, on machines it measures, behind the same OpenAI surface as
every other model.

It is a transformers loader and nothing more ambitious. The checkpoints it
serves are DualAR text-to-speech models -- a slow autoregressive transformer
emitting one semantic token per audio frame, a fast one emitting that frame's
codec codebooks, and a neural codec turning codes into a waveform -- and they
ship the whole of that as remote code in the repository. The contract this
server drives is the one their model card documents:

    inputs  = processor(text=[...], reference_audio=[...], reference_text=[...])
    codes   = model.generate(**inputs)
    wave, n = model.decode_audio(codes)

Anything whose remote code answers those three calls runs here. That is a
narrower claim than "TTS models" and it is deliberately the one the support
table in ``resolver/support.py`` makes: an architecture is listed there only
once somebody has run it through this file.

WHERE IT RUNS. Inside the model container (``docker/tts.Dockerfile``), never
in the node image -- torch, transformers, soundfile and scipy are its
dependencies and none of them are in ``requirements.txt``. Nothing else in
``control_plane/`` imports this module, and every heavy import is made inside
the function that needs it, so importing it on a coordinator with no CUDA
still works and the pure parts below stay testable there.

WHAT IT DOES NOT DO, stated rather than discovered at request time:

  * One generation at a time. The DualAR loop holds a fixed KV cache sized for
    the batch it was set up with, and this server does not micro-batch across
    requests, so ``--max-num-seqs`` bounds how many callers may be *waiting*
    and everything past that is refused with 503 rather than queued forever.
  * No streaming. The response is the whole file; a partial waveform is not a
    decodable one for any container format here.
  * No ``speed``. Resampling is not time-stretching -- it moves the pitch --
    and a request for 1.5x that quietly returned a chipmunk would be worse
    than a refusal that names what happened.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Eager, and it has to stay eager. This file has `from __future__ import
# annotations`, so `request: Request` is the *string* "Request", and FastAPI
# resolves a route's annotations against the endpoint's __globals__ -- the
# module dict. Import it inside build_app instead and the name is not there:
# the parameter is silently demoted to a required query field and every call
# comes back 422 with `{'loc': ['query','request']}`. That is the same trap
# `registry/shell_route.py` exists to document. Only torch, transformers,
# soundfile and scipy are deferred here, because those are the ones the node
# image genuinely does not have; fastapi is in requirements.txt.
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("derate.tts")

#: What ``model.generate`` is asked for when the request names nothing and the
#: window leaves room for it. 512 frames at ~21.5 frames/s is about 24 seconds
#: of speech, which is the checkpoint's own generation_config default.
DEFAULT_MAX_NEW_TOKENS = 512

#: Printed immediately before this process gives up, and the one line in this
#: file another module matches on: ``deploy/progress.py::_RUNTIME_FATAL``.
#:
#: It exists because of the rule in CLAUDE.md that a dead engine does not kill
#: its container. A solo launch execs this command inside a container that
#: sleeps forever, so when ``main`` below raises -- a checkpoint whose remote
#: code does not answer the three calls, a config with no codec, an argv the
#: parser refuses -- the process exits, the container stays up, sparkrun's
#: ``check-job`` goes on reporting the workload as running, and the launch is
#: watched for the full readiness timeout with nothing on screen naming the
#: cause. vLLM's own ``EngineCore failed to start.`` is what saves a vllm
#: launch from that; this is the same sentence for the runtime we write.
#:
#: Changing the wording means changing the copy in ``_RUNTIME_FATAL`` in the
#: same commit. ``tests/test_tts_runtime.py`` asserts the two match.
FATAL_MARKER = "fatal: the speech server failed to start."


#: The default voice library, and where it comes from.
#:
#: A voice here is a real person reading a real sentence, and the clone is
#: conditioned on the pair. That rules out synthesising the defaults from the
#: model itself -- a reference clip generated by the thing it is meant to
#: condition is a mirror, and it teaches the model nothing it does not already
#: do. It also rules out shipping them in the repository: this project keeps
#: no binary audio in the tree, and a voice is a hundred kilobytes of somebody
#: else's recording with a licence attached.
#:
#: So they are fetched, once per node, into the one writable mount. LibriTTS
#: rather than LibriSpeech, and that is the whole reason this names a corpus
#: at all: LibriSpeech transcripts are uppercase and unpunctuated, and the
#: transcript is what the clone conditions on -- "MISTER QUILTER IS THE
#: APOSTLE" is text nobody has ever said out loud. LibriTTS is the same
#: readers, cased and punctuated, and it is the corpus built for this job.
#: CC BY 4.0, hence ATTRIBUTION_NAME beside the clips.
DEFAULT_VOICE_DATASET = "mythicinfinity/libritts"
DEFAULT_VOICE_CONFIG = "clean"
DEFAULT_VOICE_SPLIT = "dev.clean"
DEFAULT_VOICE_ROWS_API = "https://datasets-server.huggingface.co/rows"

#: Which rows to take. Pinned offsets rather than a random sample, so every
#: node in a cluster ends up with the same three voices under the same names
#: -- a `voice` that means a different speaker depending on which machine
#: answered would be the worst kind of routing bug to debug. The split is
#: ordered by reader, so offsets this far apart are different people.
DEFAULT_VOICE_OFFSETS = (0, 400, 900)

#: Voices are named `libritts-<speaker id>`, which is deliberately unlovely.
#: The instinct is to call them "narrator" or "amelia", and that would be a
#: small lie about a real person: these are LibriVox readers, and a friendly
#: invented name detaches a cloned voice from whose voice it is. The id is
#: also the only handle that leads back to the corpus row if somebody asks.
VOICE_NAME_PREFIX = "libritts-"

#: A usable reference is a few seconds of connected speech. Too short and
#: there is not enough of the voice in it to clone; too long and it eats the
#: window (see MAX_REFERENCE_SECONDS). Measured in transcript characters
#: because that is what the rows API gives before anything is downloaded.
DEFAULT_VOICE_MIN_CHARS = 60
DEFAULT_VOICE_MAX_CHARS = 170

#: Bounds on the fetch itself. A default that hangs a launch is worse than no
#: default at all -- see ensure_default_voices, where every failure is a note.
DEFAULT_VOICE_TIMEOUT_S = 30.0
MAX_DEFAULT_VOICE_BYTES = 8 * 1024 * 1024

#: Written beside the clips. CC BY 4.0 requires attribution, and a directory
#: of anonymous wav files is exactly where that gets lost.
ATTRIBUTION_NAME = "ATTRIBUTION.txt"


#: Reference clips longer than this are refused rather than truncated. A long
#: reference eats the same 2048-position window the text and the generated
#: audio have to share, and the model card names long clips as the thing that
#: destabilises a clone.
MAX_REFERENCE_SECONDS = 30.0


# ── response formats ─────────────────────────────────────────────────────────
#
# OpenAI names six. Five are reachable through libsndfile and one is not, so
# `aac` is refused by name instead of being silently served as something else
# -- a client that asked for AAC and got MP3 under `Content-Type: audio/aac`
# would fail somewhere much further from here.
#
# `opus` is the awkward one: libsndfile's Opus writer accepts 8/12/16/24/48 kHz
# only, and these checkpoints emit 44.1 kHz. It is therefore the one format
# that needs a resample, which is why scipy is a dependency of the image and
# why a build without it refuses `opus` specifically rather than failing to
# start.


@dataclass(frozen=True)
class AudioFormat:
    """One value of ``response_format``, and how to write it."""

    name: str
    #: libsndfile container and subtype.
    container: str
    subtype: str
    content_type: str
    #: Sample rates this container accepts, or () for "whatever it is given".
    #: Non-empty means the waveform is resampled to the nearest one first.
    rates: tuple[int, ...] = ()


FORMATS: dict[str, AudioFormat] = {
    "wav": AudioFormat("wav", "WAV", "PCM_16", "audio/wav"),
    "mp3": AudioFormat("mp3", "MP3", "MPEG_LAYER_III", "audio/mpeg"),
    "flac": AudioFormat("flac", "FLAC", "PCM_16", "audio/flac"),
    "opus": AudioFormat(
        "opus", "OGG", "OPUS", "audio/ogg", rates=(8000, 12000, 16000, 24000, 48000)
    ),
    # Headerless, so the rate travels in a header rather than in the bytes.
    # OpenAI's own pcm is 24 kHz; ours is whatever the codec produces, and
    # X-Audio-Sample-Rate on the response says which.
    "pcm": AudioFormat("pcm", "RAW", "PCM_16", "audio/pcm"),
}

#: Named so the refusal can say what happened rather than "unsupported".
UNAVAILABLE_FORMATS: dict[str, str] = {
    "aac": (
        "libsndfile, which is this server's only encoder, cannot write AAC. "
        "Ask for mp3 or opus if the transport needs a lossy codec, or flac "
        "or wav if it does not"
    ),
}

DEFAULT_FORMAT = "mp3"


def resolve_format(name: str | None) -> AudioFormat:
    """The format to write, or ValueError carrying the reason.

    The message names the formats that would have worked, because
    "unsupported format" leaves the caller guessing which of the six they may
    have.
    """
    key = (name or DEFAULT_FORMAT).strip().lower()
    found = FORMATS.get(key)
    if found is not None:
        return found
    known = ", ".join(sorted(FORMATS))
    reason = UNAVAILABLE_FORMATS.get(key)
    if reason:
        raise ValueError(f"response_format {key!r}: {reason}. Available: {known}.")
    raise ValueError(
        f"response_format {key!r} is not a format this server writes. "
        f"Available: {known}."
    )


def encode_audio(samples, sample_rate: int, fmt: AudioFormat) -> tuple[bytes, int]:
    """Encode float samples to *fmt*. Returns (bytes, the rate written).

    The rate comes back because ``opus`` changes it: the caller reports the
    real one on the response rather than the one the model produced.
    """
    import soundfile  # noqa: PLC0415 -- container-only dependency

    rate = sample_rate
    if fmt.rates and sample_rate not in fmt.rates:
        target = min(fmt.rates, key=lambda r: (abs(r - sample_rate), -r))
        samples = _resample(samples, sample_rate, target)
        rate = target

    buffer = io.BytesIO()
    soundfile.write(
        buffer, samples, rate, format=fmt.container, subtype=fmt.subtype
    )
    return buffer.getvalue(), rate


def _resample(samples, source_rate: int, target_rate: int):
    """Rational resampling, or ValueError naming what is missing.

    ``resample_poly`` and not an interpolation written here: 44.1 kHz to 48 kHz
    is 160/147, and a naive linear resample of speech at that ratio is audibly
    worse than the format it was asked for.
    """
    if source_rate == target_rate:
        return samples
    try:
        from math import gcd  # noqa: PLC0415

        from scipy.signal import resample_poly  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on the image
        raise ValueError(
            f"this format needs {target_rate} Hz and the model produces "
            f"{source_rate} Hz, but scipy is not installed in this image so "
            f"there is no resampler to bridge them. Ask for wav, flac or mp3, "
            f"which take {source_rate} Hz as it is"
        ) from exc
    divisor = gcd(source_rate, target_rate)
    return resample_poly(samples, target_rate // divisor, source_rate // divisor)


# ── voices ───────────────────────────────────────────────────────────────────
#
# These checkpoints clone zero-shot from a reference clip, so a "voice" here is
# a recording plus its exact transcript -- the model card is explicit that a
# transcript which does not match the audio degrades the clone. A directory of
# `<name>.wav` beside `<name>.txt` is therefore the whole registry, and a clip
# with no transcript is not offered rather than offered badly.
#
# Naming no voice at all is a real request and not an error: these models
# generate in their own voice with no reference, which is what a caller who
# just wants speech wants.


@dataclass(frozen=True)
class Voice:
    name: str
    audio_path: Path
    transcript: str


@dataclass
class VoiceLibrary:
    """The voices this deployment can clone. Possibly none.

    **A startup snapshot, not a view of the directory.** `SpeechEngine` builds
    this once in `load()` and never rebuilds it, so `GET /v1/audio/voices`
    reports what this server believed when it started and not what is on disk
    now. Delete a voice under a running deployment and it stays advertised and
    answers 500 on use; add one and it is invisible until the next launch.
    Stated here rather than only in 00-architecture.md because this class is
    where somebody will be standing when they hit it, and it is why
    `ensure_default_voices` runs before `load()` rather than after.
    """

    voices: dict[str, Voice] = field(default_factory=dict)
    #: Clips that were found and rejected, and why. Reported at startup and in
    #: the refusal, so a mistyped transcript file is visible rather than a
    #: voice that silently is not there.
    skipped: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return sorted(self.voices)

    def resolve(self, name: str | None) -> Voice | None:
        """The voice to clone, None for the model's own, or ValueError.

        An unknown name is refused rather than quietly falling back to the
        default speaker: the caller asked for a specific voice, and returning
        a different one under a 200 is the failure they cannot see.
        """
        wanted = (name or "").strip()
        if not wanted:
            return None
        found = self.voices.get(wanted)
        if found is not None:
            return found
        if self.voices:
            raise ValueError(
                f"voice {wanted!r} is not installed on this deployment. "
                f"Installed: {', '.join(self.names)}. Omit `voice` entirely "
                f"to generate in the model's own voice."
            )
        raise ValueError(
            f"voice {wanted!r} is not installed: this deployment has no voice "
            f"library at all. Omit `voice` to generate in the model's own "
            f"voice, or start the runtime with --voice-dir pointing at a "
            f"directory of <name>.wav files, each beside a <name>.txt holding "
            f"that clip's exact transcript."
        )


def _fetch_json(url: str, timeout: float) -> dict:
    """One JSON GET. Split out so a test can substitute it."""
    import urllib.request  # noqa: PLC0415

    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _fetch_bytes(url: str, timeout: float, limit: int) -> bytes:
    """One bounded GET. Reads `limit` + 1 and refuses at the cap rather than
    trusting a content-length nobody sent."""
    import urllib.request  # noqa: PLC0415

    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"clip is over {limit} bytes")
    return data


def _rows_url(offset: int, length: int = 8) -> str:
    import urllib.parse  # noqa: PLC0415

    query = urllib.parse.urlencode(
        {
            "dataset": DEFAULT_VOICE_DATASET,
            "config": DEFAULT_VOICE_CONFIG,
            "split": DEFAULT_VOICE_SPLIT,
            "offset": offset,
            "length": length,
        }
    )
    return f"{DEFAULT_VOICE_ROWS_API}?{query}"


def _pick_row(rows: list, taken: set[str]) -> tuple[str, str, str] | None:
    """(speaker, transcript, clip url) for the first usable row, or None.

    "Usable" is a transcript of a few seconds, from a reader we have not
    already taken. Deliberately the *first* match rather than the best one:
    the point is that every node picks the same row from the same offset.
    """
    for row in rows:
        record = row.get("row") if isinstance(row, dict) and "row" in row else row
        if not isinstance(record, dict):
            continue
        speaker = str(record.get("speaker_id") or "").strip()
        transcript = str(record.get("text_normalized") or record.get("text") or "").strip()
        audio = record.get("audio")
        source = ""
        if isinstance(audio, list) and audio and isinstance(audio[0], dict):
            source = str(audio[0].get("src") or "")
        if not speaker or not source or speaker in taken:
            continue
        if not DEFAULT_VOICE_MIN_CHARS <= len(transcript) <= DEFAULT_VOICE_MAX_CHARS:
            continue
        return speaker, transcript, source
    return None


def ensure_default_voices(
    directory: str | os.PathLike[str] | None,
    *,
    enabled: bool = True,
    fetch_json=_fetch_json,
    fetch_bytes=_fetch_bytes,
) -> list[str]:
    """Put a starter voice library in `directory`, and never fail because of it.

    Returns notes, which the caller logs. Every one of them is informational:
    with no voices this server still speaks in the checkpoint's own, which is
    exactly what it did before this function existed. A launch that died
    because a dataset API was slow would be a launch lost to a nicety.

    **An existing library is never touched.** One usable pair in the directory
    and this does nothing at all -- an operator who installed their own voices
    has said what they want, and quietly adding three strangers beside them
    would be the surprising thing. That also makes it idempotent per node:
    the directory is `RUNTIME_CACHE_DIR/voices`, the one mount that survives
    the container, so the fetch happens once per machine and not once per
    launch.

    Writes are atomic and transcript-last: the `.txt` is what `load_voices`
    requires beside a clip, so a crash mid-fetch leaves an audio file with no
    transcript, which that function already skips with a reason. The reverse
    order would leave a transcript pointing at nothing.
    """
    try:
        return _ensure_default_voices(directory, enabled, fetch_json, fetch_bytes)
    except Exception as exc:  # noqa: BLE001
        # The last line of defence, and it is not decoration. `main` wraps
        # parse-load-build in a try that prints FATAL_MARKER and ends the
        # launch, so an exception escaping this function would turn a slow
        # dataset API into a failed deployment. Nothing about a starter voice
        # library is worth a launch.
        return [f"default voices unavailable: {type(exc).__name__}: {exc}"]


def _ensure_default_voices(directory, enabled, fetch_json, fetch_bytes) -> list[str]:
    notes: list[str] = []
    if not enabled:
        return ["default voices are switched off; the library is whatever is on disk"]
    if not directory:
        return ["no voice directory, so there is nowhere to put default voices"]

    root = Path(directory)
    existing = load_voices(root)
    if existing.voices:
        return [
            f"{root} already has {len(existing.voices)} voice(s); "
            f"leaving the library alone"
        ]
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [f"cannot create {root}: {exc}"]

    taken: set[str] = set()
    written = 0
    for offset in DEFAULT_VOICE_OFFSETS:
        try:
            payload = fetch_json(_rows_url(offset), DEFAULT_VOICE_TIMEOUT_S)
            picked = _pick_row(payload.get("rows") or [], taken)
            if picked is None:
                notes.append(f"no usable row at offset {offset}")
                continue
            speaker, transcript, source = picked
            clip = fetch_bytes(source, DEFAULT_VOICE_TIMEOUT_S, MAX_DEFAULT_VOICE_BYTES)
            name = f"{VOICE_NAME_PREFIX}{speaker}"
            audio_path = root / f"{name}.wav"
            tmp = audio_path.with_suffix(".wav.tmp")
            tmp.write_bytes(clip)
            tmp.replace(audio_path)
            (root / f"{name}.txt").write_text(transcript + "\n", encoding="utf-8")
            taken.add(speaker)
            written += 1
            notes.append(f"installed {name} ({len(clip)} bytes)")
        except Exception as exc:  # noqa: BLE001 - every failure here is a note
            notes.append(f"could not install a voice from offset {offset}: {exc}")

    if written:
        try:
            (root / ATTRIBUTION_NAME).write_text(
                "The .wav/.txt pairs named libritts-* in this directory are\n"
                f"excerpts from {DEFAULT_VOICE_DATASET} ({DEFAULT_VOICE_CONFIG}/"
                f"{DEFAULT_VOICE_SPLIT}), which is LibriTTS.\n"
                "LibriTTS is licensed CC BY 4.0 and derives from LibriVox\n"
                "public-domain audiobook recordings.\n"
                "https://creativecommons.org/licenses/by/4.0/\n"
                "\n"
                "They are installed automatically by\n"
                "control_plane/runtimes/tts.py::ensure_default_voices. Delete\n"
                "them, or put your own <name>.wav beside a <name>.txt here, and\n"
                "nothing will replace them.\n",
                encoding="utf-8",
            )
        except OSError as exc:
            notes.append(f"could not write {ATTRIBUTION_NAME}: {exc}")
    return notes


def load_voices(directory: str | os.PathLike[str] | None) -> VoiceLibrary:
    """Read a voice directory. A missing directory is empty, not an error."""
    library = VoiceLibrary()
    if not directory:
        return library
    root = Path(directory)
    if not root.is_dir():
        library.skipped.append(f"{root} is not a directory; no voices loaded")
        return library

    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in (".wav", ".flac", ".mp3", ".ogg"):
            continue
        transcript_path = path.with_suffix(".txt")
        if not transcript_path.is_file():
            library.skipped.append(
                f"{path.name}: no {transcript_path.name} beside it, so there "
                f"is no transcript to condition the clone on"
            )
            continue
        transcript = transcript_path.read_text(encoding="utf-8").strip()
        if not transcript:
            library.skipped.append(f"{transcript_path.name} is empty")
            continue
        too_long = _too_long(path)
        if too_long:
            library.skipped.append(too_long)
            continue
        library.voices[path.stem] = Voice(path.stem, path, transcript)
    return library


def _too_long(path: Path) -> str | None:
    """Why this clip is not usable as a reference, or None.

    Length is the one property worth checking before the GPU is involved: the
    reference is packed into the same window the text and the generated audio
    share, so a two-minute clip does not clone badly, it leaves no room to
    speak at all. Skipped silently when there is no decoder to ask -- this
    runs at startup and a missing soundfile is a much louder failure two lines
    later.
    """
    try:
        import soundfile  # noqa: PLC0415
    except ImportError:  # pragma: no cover - depends on the image
        return None
    try:
        info = soundfile.info(str(path))
    except Exception as exc:  # unreadable, or a format libsndfile will not open
        return f"{path.name}: cannot be read as audio ({exc})"
    if info.duration > MAX_REFERENCE_SECONDS:
        return (
            f"{path.name}: {info.duration:.0f}s of reference, over the "
            f"{MAX_REFERENCE_SECONDS:.0f}s limit -- it shares the model's "
            f"window with the text and the speech being generated"
        )
    return None


# ── the engine ───────────────────────────────────────────────────────────────


@dataclass
class ServerConfig:
    """Everything the recipe's command line decides."""

    model: str
    served_model_name: str
    host: str = "0.0.0.0"
    port: int = 8000
    max_model_len: int = 2048
    max_num_seqs: int = 1
    gpu_memory_utilization: float = 0.90
    tensor_parallel_size: int = 1
    pipeline_parallel_size: int = 1
    trust_remote_code: bool = False
    voice_dir: str | None = None
    #: Fetch a starter library when `voice_dir` is empty. See
    #: ensure_default_voices; off leaves the directory exactly as found.
    default_voices: bool = True
    device: str | None = None


def _patch_reference_text_tag(processor: Any) -> None:
    """Undo one specific defect in the checkpoint's own `processing_arktts.py`,
    on the loaded instance only.

    Every reference-conditioned request runs the reference transcript through
    `ArkttsProcessor._format_reference_text`, which prepends `<|speaker:0|>` --
    a tag that mirrors the `<|semantic:N|>` convention (same brackets, same
    colon, and the code even guards against double-prepending it) but was
    never added to this checkpoint's tokenizer vocabulary. Confirmed directly:
    `tokenizer.encode("<|speaker:0|>", add_special_tokens=False)` comes back as
    seven ordinary sub-word ids (``<``, ``|``, ``speaker``, ``:``, ``0``, ``|``,
    ``>``) instead of the one atomic id every other structural tag gets
    (`<|im_start|>`, `<|voice|>`, and all 4,096 `<|semantic:N|>` round-trip as a
    single token). The result is seven garbage tokens spliced into the prompt
    immediately before the real reference sentence, on every cloned voice,
    every time -- clearly unintended, and worth removing regardless of what
    else is going on.

    That said: an instrumented A/B (`tests/tts_diagnose.py --phase 5/6`, 15+
    trials through this exact class) found removing the tag does **not** fix
    the separate, occasional (roughly a few percent of calls, worse for some
    reference clips) collapse where generation samples end-of-speech after a
    handful of frames -- if anything the sample skewed slightly worse without
    it. That collapse remains open; see 00-architecture.md's TTS appendix. This
    patch is scoped to the one confirmed defect: a garbage token sequence that
    has no business being in the prompt.

    A duck-typed guard, not a hard dependency: a future checkpoint revision
    that fixes its own vocabulary, or a different checkpoint entirely, simply
    has no `_format_reference_text` to find, and this is a no-op.
    """
    if not hasattr(processor, "_format_reference_text"):
        return

    def _without_the_broken_tag(text: str) -> str:
        return " ".join(str(text).strip().split())

    processor._format_reference_text = _without_the_broken_tag


class SpeechEngine:
    """One checkpoint, loaded once, generating one request at a time."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.voices = load_voices(config.voice_dir)
        self.model: Any = None
        self.processor: Any = None
        self.device: str = config.device or "cpu"
        self.sample_rate: int = 0
        #: The real position budget: the smaller of what the recipe asked for
        #: and what the checkpoint can hold. Reported at startup when they
        #: differ, because the launcher chose the first and the second is the
        #: one that will actually refuse a long request.
        self.window: int = config.max_model_len
        self._gpu_lock = threading.Lock()

    # -- loading ----------------------------------------------------------

    def load(self) -> None:
        """Load the checkpoint, the codec, and nothing lazily.

        The codec is warmed here rather than on first request: it is a third
        of the memory this process holds, and a deployment that reports READY
        before allocating it would pass the health check and then fail the
        first synthesis with an out-of-memory error nothing on screen could
        connect to the launch.
        """
        import torch  # noqa: PLC0415
        from transformers import AutoModel, AutoProcessor  # noqa: PLC0415

        started = time.monotonic()
        if self.config.device:
            self.device = self.config.device
        else:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.bfloat16 if self.device.startswith("cuda") else torch.float32

        if self.device.startswith("cuda") and 0 < self.config.gpu_memory_utilization < 1:
            # A real cap and not a decoration: this process has no paged KV
            # pool to size, so the fraction is applied to the allocator
            # itself. It is what keeps a TTS deployment inside the budget the
            # fit gate cleared it for when it shares a machine.
            try:
                torch.cuda.set_per_process_memory_fraction(
                    self.config.gpu_memory_utilization
                )
            except (RuntimeError, ValueError) as exc:
                log.warning(
                    "could not apply --gpu-memory-utilization %.2f: %s",
                    self.config.gpu_memory_utilization,
                    exc,
                )

        self.processor = AutoProcessor.from_pretrained(
            self.config.model, trust_remote_code=self.config.trust_remote_code
        )
        _patch_reference_text_tag(self.processor)
        self.model = (
            AutoModel.from_pretrained(
                self.config.model,
                trust_remote_code=self.config.trust_remote_code,
                dtype=dtype,
            )
            .eval()
            .to(self.device)
        )

        native = int(getattr(self.model.config, "max_seq_len", 0) or 0)
        if native and native < self.window:
            log.warning(
                "--max-model-len %d is above this checkpoint's own %d packed "
                "positions; requests are bounded by %d",
                self.window,
                native,
                native,
            )
            self.window = native
        self.sample_rate = int(getattr(self.model.config, "codec_sample_rate", 0) or 0)
        if not self.sample_rate:
            raise RuntimeError(
                f"{self.config.model} declares no codec_sample_rate, so there "
                f"is no rate to write the audio at. This runtime serves DualAR "
                f"speech checkpoints; a model without a codec is not one."
            )

        # Force the codec resident now. load_codec is the checkpoint's own
        # lazy hook and decode_audio is the only caller, so a zero-length
        # decode is the cheapest way to make it allocate.
        loader = getattr(self.model, "load_codec", None)
        if callable(loader):
            loader(device=self.device)

        log.info(
            "loaded %s on %s in %.1fs: %d Hz, %d positions, %d voice(s)",
            self.config.model,
            self.device,
            time.monotonic() - started,
            self.sample_rate,
            self.window,
            len(self.voices.voices),
        )
        for note in self.voices.skipped:
            log.warning("voice skipped -- %s", note)

    # -- synthesis --------------------------------------------------------

    def synthesize(
        self,
        text: str,
        voice: Voice | None,
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        max_new_tokens: int | None = None,
    ):
        """Text to samples. Runs on a worker thread; holds the GPU lock.

        Returns a 1-D float32 numpy array at ``self.sample_rate``.
        """
        import torch  # noqa: PLC0415

        inputs: dict[str, Any] = {"text": [text], "return_tensors": "pt"}
        if voice is not None:
            inputs["reference_audio"] = [str(voice.audio_path)]
            inputs["reference_text"] = [voice.transcript]

        with self._gpu_lock:
            batch = self.processor(**inputs)
            batch = {
                name: value.to(self.device)
                for name, value in batch.items()
                if hasattr(value, "to")
            }
            with torch.inference_mode():
                codes = self.model.generate(
                    **batch,
                    max_new_tokens=max_new_tokens or DEFAULT_MAX_NEW_TOKENS,
                    temperature=temperature,
                    top_p=top_p,
                    do_sample=True,
                )
                waveforms, lengths = self.model.decode_audio(codes)
        samples = waveforms[0, : int(lengths[0])].float().cpu().numpy()
        return samples


# ── admission ────────────────────────────────────────────────────────────────


class Admission:
    """How many callers may be in flight, counted rather than queued.

    One generation runs at a time whatever this says; the number bounds how
    many others may be waiting for the lock. Past it the answer is 503 with a
    Retry-After, which the gateway's parking lot already knows what to do with
    -- an unbounded queue would instead hold every caller until their client
    timed out, and report nothing about it.
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._in_flight = 0

    def try_enter(self) -> bool:
        with self._lock:
            if self._in_flight >= self.limit:
                return False
            self._in_flight += 1
            return True

    def leave(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight


# ── request validation ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SpeechRequest:
    """A validated ``POST /v1/audio/speech`` body."""

    model: str
    input: str
    voice: str | None
    fmt: AudioFormat
    temperature: float | None
    top_p: float | None
    max_new_tokens: int | None


def parse_speech_request(body: Any, served_name: str) -> SpeechRequest:
    """Validate one request body, or ValueError carrying the whole reason.

    Pure, so the refusals it writes are testable without a model on the GPU.
    The model name is checked here too: this server holds exactly one
    checkpoint, and answering for a name it does not serve would make a
    misrouted request look like a working one.
    """
    if not isinstance(body, dict):
        raise ValueError("the request body must be a JSON object")

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("you must provide a 'model' parameter")
    if model.strip() not in (served_name, ""):
        raise ValueError(
            f"this deployment serves {served_name!r}, not {model.strip()!r}"
        )

    text = body.get("input")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("you must provide a non-empty 'input' to speak")

    speed = body.get("speed")
    if speed is not None and float(speed) != 1.0:
        raise ValueError(
            f"speed {float(speed)} is not available: this server has no "
            f"time-stretch, and resampling to fake one would move the pitch "
            f"as well as the rate. Generate at 1.0 and change the speed in "
            f"the player, which does it properly"
        )

    voice = body.get("voice")
    if voice is not None and not isinstance(voice, str):
        raise ValueError("'voice' must be a string naming an installed voice")

    fmt = resolve_format(body.get("response_format"))

    return SpeechRequest(
        model=model.strip(),
        input=text,
        voice=voice,
        fmt=fmt,
        temperature=_optional_float(body, "temperature"),
        top_p=_optional_float(body, "top_p"),
        max_new_tokens=_optional_int(body, "max_new_tokens"),
    )


def _optional_float(body: dict, key: str) -> float | None:
    value = body.get(key)
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{key}' must be a number") from None
    if parsed <= 0:
        raise ValueError(f"'{key}' must be greater than zero")
    return parsed


def _optional_int(body: dict, key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{key}' must be an integer") from None
    if parsed <= 0:
        raise ValueError(f"'{key}' must be greater than zero")
    return parsed


def error_body(message: str, code: str, status: int) -> dict:
    """OpenAI's error envelope, which is what every client here already parses."""
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error" if status < 500 else "server_error",
            "param": None,
            "code": code,
        }
    }


# ── the app ──────────────────────────────────────────────────────────────────


def build_app(engine: SpeechEngine, admission: Admission):
    """The HTTP surface: /health, /v1/models, /v1/audio/speech.

    ``/health`` and ``/v1/models`` are not decoration -- ``deploy/health.py``
    probes exactly those two paths in that order, and a runtime answering
    neither never leaves LAUNCHING.
    """
    app = FastAPI(title="derate tts", docs_url=None, redoc_url=None)
    served = engine.config.served_model_name

    @app.get("/health")
    async def health() -> Response:
        return Response(status_code=200)

    @app.get("/v1/models")
    async def models() -> JSONResponse:
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": served,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "derate",
                        # The one field a client cannot guess from the name,
                        # and the same word the gateway's own /v1/models uses.
                        "modality": "speech",
                    }
                ],
            }
        )

    @app.get("/v1/audio/voices")
    async def voices() -> JSONResponse:
        """Not an OpenAI route. There is no way to ask that API what voices
        exist, because its voices are fixed; a cloning deployment's are not,
        and a caller with no way to enumerate them can only guess."""
        return JSONResponse(
            {
                "object": "list",
                "data": [{"id": name} for name in engine.voices.names],
                "skipped": list(engine.voices.skipped),
            }
        )

    @app.post("/v1/audio/speech")
    async def speech(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "the request body is not valid JSON", "invalid_json", 400
                ),
            )

        try:
            parsed = parse_speech_request(body, served)
            voice = engine.voices.resolve(parsed.voice)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content=error_body(str(exc), "invalid_request", 400),
            )

        if not admission.try_enter():
            return JSONResponse(
                status_code=503,
                content=error_body(
                    f"this deployment is generating and {admission.limit} "
                    f"request(s) are already waiting; it synthesises one at a "
                    f"time",
                    "server_busy",
                    503,
                ),
                headers={"Retry-After": "1"},
            )
        started = time.monotonic()
        try:
            samples = await run_in_threadpool(
                engine.synthesize,
                parsed.input,
                voice,
                temperature=parsed.temperature,
                top_p=parsed.top_p,
                max_new_tokens=parsed.max_new_tokens,
            )
            audio, rate = await run_in_threadpool(
                encode_audio, samples, engine.sample_rate, parsed.fmt
            )
        except ValueError as exc:
            # The checkpoint's own refusals land here -- a prompt longer than
            # the window is the common one -- and they are forwarded verbatim
            # rather than rewritten, for the reason every other refusal in
            # this project is: it already names what to change.
            return JSONResponse(
                status_code=400,
                content=error_body(str(exc), "invalid_request", 400),
            )
        except Exception as exc:  # pragma: no cover - hardware failures
            log.exception("synthesis failed")
            return JSONResponse(
                status_code=500,
                content=error_body(
                    f"synthesis failed: {exc}", "synthesis_failed", 500
                ),
            )
        finally:
            admission.leave()

        seconds = len(samples) / float(engine.sample_rate or 1)
        elapsed = time.monotonic() - started
        log.info(
            "spoke %d chars as %.2fs of audio in %.2fs (%.2fx real time) as %s",
            len(parsed.input),
            seconds,
            elapsed,
            seconds / elapsed if elapsed else 0.0,
            parsed.fmt.name,
        )
        return Response(
            content=audio,
            media_type=parsed.fmt.content_type,
            headers={
                # Headerless PCM carries its rate nowhere else, and every
                # other format is cheaper to read here than to parse.
                "X-Audio-Sample-Rate": str(rate),
                "X-Audio-Duration-Seconds": "%.3f" % seconds,
            },
        )

    return app


# ── command line ─────────────────────────────────────────────────────────────
#
# The flags mirror `vllm serve` where the concept exists, because
# deploy/flags.py emits the same knobs for every runtime and a knob that names
# nothing in the command template is dropped in silence -- the exact failure
# that file's header warns about. Every flag below is therefore either used or
# refused; none is accepted and ignored.


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m control_plane.runtimes.tts",
        description="Serve a DualAR text-to-speech checkpoint on /v1/audio/speech.",
    )
    parser.add_argument("--model", required=True, help="HuggingFace id or local path")
    parser.add_argument("--served-model-name", default=None)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--max-model-len",
        type=int,
        default=2048,
        help="packed text+audio positions; clamped to the checkpoint's own limit",
    )
    parser.add_argument(
        "--max-num-seqs",
        type=int,
        default=1,
        help="requests that may be in flight; past this the answer is 503",
    )
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.90)
    parser.add_argument("--tensor-parallel-size", type=int, default=1)
    parser.add_argument("--pipeline-parallel-size", type=int, default=1)
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument(
        "--voice-dir",
        default=os.environ.get("DERATE_TTS_VOICE_DIR"),
        help="directory of <name>.wav files, each beside a <name>.txt transcript",
    )
    parser.add_argument(
        "--no-default-voices",
        dest="default_voices",
        action="store_false",
        help="do not fetch a starter voice library into an empty --voice-dir "
             "(env DERATE_TTS_DEFAULT_VOICES=0)",
    )
    parser.set_defaults(
        default_voices=os.environ.get("DERATE_TTS_DEFAULT_VOICES", "1") not in ("0", "false", "no")
    )
    parser.add_argument("--device", default=None, help="cuda, cuda:1, cpu")
    return parser


def config_from_args(args: argparse.Namespace) -> ServerConfig:
    """Turn parsed flags into a config, refusing what this server cannot do.

    Sharding is the case that matters. This is one process holding one
    checkpoint, and a --tp 2 accepted here would serve half a model's worth of
    nothing on a machine the planner had already committed. It is refused at
    startup, where sparkrun's own liveness check turns it into a FAILED
    deployment with this sentence in the log, rather than at the first
    request.
    """
    for flag, value in (
        ("--tensor-parallel-size", args.tensor_parallel_size),
        ("--pipeline-parallel-size", args.pipeline_parallel_size),
    ):
        if int(value) != 1:
            raise SystemExit(
                f"{flag} is {value}: this runtime runs one process on one GPU "
                f"and cannot shard a checkpoint. Plan this deployment at TP=1 "
                f"PP=1, or serve it on a runtime that shards."
            )
    return ServerConfig(
        model=args.model,
        served_model_name=args.served_model_name or args.model,
        host=args.host,
        port=args.port,
        max_model_len=args.max_model_len,
        max_num_seqs=args.max_num_seqs,
        gpu_memory_utilization=args.gpu_memory_utilization,
        tensor_parallel_size=int(args.tensor_parallel_size),
        pipeline_parallel_size=int(args.pipeline_parallel_size),
        trust_remote_code=bool(args.trust_remote_code),
        voice_dir=args.voice_dir,
        default_voices=bool(args.default_voices),
        device=args.device,
    )


def main(argv: list[str] | None = None) -> int:
    """Parse, load, serve -- and say so out loud if any of the three fails.

    Everything before ``uvicorn.run`` happens with no HTTP surface up, so a
    failure in it is invisible to the health probe and to `check-job` alike.
    `FATAL_MARKER` is what makes it visible; see that constant for why the
    container's own liveness is not enough.
    """
    import uvicorn  # noqa: PLC0415

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        config = config_from_args(build_parser().parse_args(argv))
        # Before SpeechEngine, because the engine reads the voice directory in
        # its constructor and never looks again -- see VoiceLibrary below.
        for note in ensure_default_voices(
            config.voice_dir, enabled=config.default_voices
        ):
            log.info("default voices: %s", note)
        engine = SpeechEngine(config)
        engine.load()
        app = build_app(engine, Admission(config.max_num_seqs))
    except SystemExit as exc:
        # argparse exits 0 for --help and 2 for a bad argv, and
        # config_from_args exits with a sentence. Only the last two are a
        # launch dying; `--help` printing and leaving is not, and marking it
        # fatal would end a launch nobody started.
        if exc.code not in (0, None):
            log.error("%s %s", FATAL_MARKER, exc)
        raise
    except BaseException:
        # BaseException on purpose: KeyboardInterrupt during a load is still
        # this process on its way out, and the watcher needs to stop waiting
        # for a server that will never bind.
        log.exception(FATAL_MARKER)
        raise
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
