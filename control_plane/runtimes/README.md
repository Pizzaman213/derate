# runtimes

The inference servers derate ships itself, rather than launches. Everything
here runs **inside a model container** built from `docker/tts.Dockerfile` —
never in the node image — because torch, transformers, soundfile and scipy are
its dependencies and not one of them is in `requirements.txt`. Nothing else in
`control_plane/` imports this package, and every heavy import is made inside
the function that needs it, so the pure parts stay testable on a coordinator
with no CUDA.

There is exactly one server here, and it exists because the alternative was
nothing: `tts` answers `POST /v1/audio/speech`, which vLLM does not serve and
SGLang does not serve, and whose checkpoints are in neither project's
supported-architecture list. The gateway had proxied that route since audio
landed and no deployment in the cluster could answer it.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `tts.py` | 1191 | the text-to-speech server: formats, voices, the engine, admission, request validation, the app, and the command line |
| `__init__.py` | 12 | the isolation rule, stated where an importer will read it |

## `tts.py`

**A transformers loader and nothing more ambitious.** It serves DualAR
speech checkpoints — a slow autoregressive transformer emitting one semantic
token per audio frame, a fast one emitting that frame's codec codebooks, and a
bundled neural codec — and it drives exactly the three calls their model cards
document:

```python
inputs  = processor(text=[...], reference_audio=[...], reference_text=[...])
codes   = model.generate(**inputs)
wave, n = model.decode_audio(codes)
```

Anything whose remote code answers those three runs here. That is a narrower
claim than "TTS models", and it is the claim `resolver/support.py`'s
`TTS_ARCHITECTURES` makes: one name today, `ArkttsModel`, verified on a GB10
against `Audio8/Audio8-TTS-Preview-0.6b`. A name goes in that set after
somebody has run that checkpoint through this file, never because a model card
says TTS — a model listed and not loadable is a launch that clears every gate
and dies at load.

`main(argv)` is the entry point: `build_parser()` → `config_from_args()` →
`ensure_default_voices()` → `SpeechEngine(config)` → `.load()` → `build_app()`
→ `uvicorn.run`. The constructor is a step in that chain and not a formality —
it is where `load_voices` runs, which is why the starter fetch has to come
before it and not after. `build_app` serves `/health`, `/v1/models`,
`/v1/audio/voices` and `POST /v1/audio/speech`; `ServerConfig` is the frozen-in-
practice record of everything the command line decided.

### `fastapi` is imported eagerly, and it has to stay that way

This file has `from __future__ import annotations`, so `request: Request` is
the *string* `"Request"`, and FastAPI resolves a route's annotations against
the endpoint's `__globals__` — the module dict. Move that import inside
`build_app` and the name is not there: the parameter is silently demoted to a
required query field and every call comes back 422 with
`{'loc': ['query','request']}`. Only torch, transformers, soundfile and scipy
are deferred, because those are the four the node image genuinely does not
have.

### `FATAL_MARKER` is why a dead launch says so

`FATAL_MARKER = "fatal: the speech server failed to start."` is printed
immediately before this process gives up, and it is the one line in this file
another module matches on. A solo sparkrun launch execs this command inside a
container that sleeps forever, so when the parse-load-build ahead of
`uvicorn.run` raises — a checkpoint whose remote code answers something else, a
config with no `codec_sample_rate`, an argv the parser refuses — the process
exits, the container stays up, `check-job` goes on reporting the workload as
running, and the launch is watched for the full readiness timeout with nothing
on screen naming the cause. `deploy/progress.py::_RUNTIME_FATAL` carries a
verbatim copy of the string — a copy and not an import, because `progress.py`
runs on a node that must never import this package — and the assertion holding
the two together is `assert tts.FATAL_MARKER in progress._RUNTIME_FATAL` in
`tests/test_deploy.py`. Follow `tts.py`'s own docstring instead and you will
look for it in `tests/test_tts_runtime.py`, which does not import `progress`
and does not check this.

`main` splits `SystemExit` by code: 0 and `None` are `--help` printing and
leaving, which is not a launch dying and must not be marked fatal. Everything
else, `BaseException` included, is logged with the marker — a `KeyboardInterrupt`
during a load is still a process on its way out, and the watcher needs to stop
waiting for a server that will never bind.

### Five formats, and `aac` is refused by name

`resolve_format(name)` is the only way in: `FORMATS` holds `wav`, `mp3`,
`flac`, `opus` and `pcm`, and `DEFAULT_FORMAT` is `mp3`, so a body naming no
`response_format` gets MP3. OpenAI names six, and libsndfile — this server's
only encoder — cannot write the sixth, so `aac` is in `UNAVAILABLE_FORMATS`
with a sentence saying which encoder is missing and what to ask for instead.
Serving MP3 bytes under `Content-Type: audio/aac` would fail somewhere much
further from here.

`opus` is the awkward one and the reason scipy is in the image: libsndfile's
Opus writer accepts 8/12/16/24/48 kHz only and these checkpoints emit 44.1 kHz,
so `encode_audio` resamples through `_resample`, which uses
`scipy.signal.resample_poly` rather than an interpolation written here — 44.1
to 48 kHz is 160/147, and a naive linear resample of speech at that ratio is
audibly worse than the format it was asked for. `encode_audio` returns the rate
it actually wrote, and the response carries it in `X-Audio-Sample-Rate` beside
`X-Audio-Duration-Seconds`, because headerless `pcm` carries its rate nowhere
else.

### A voice is a file pair, and an unknown one is refused

These checkpoints clone zero-shot from a reference clip *plus that clip's exact
transcript*, so `load_voices` reads a directory of `<name>.wav` — or `.flac`,
`.mp3`, `.ogg`; four suffixes, not the one the `--voice-dir` help text names —
beside `<name>.txt`, and that pair is the whole registry. The stem is the voice
name, so `libritts-1272.wav` is asked for as `libritts-1272`.

A clip with no transcript, an empty transcript, or one `_too_long` measures
over `MAX_REFERENCE_SECONDS` (30.0) lands in `VoiceLibrary.skipped` with the
reason and is not offered — a long reference does not clone badly, it shares
the same 2048-position window as the text and the speech being generated and
leaves no room to speak. `_too_long` asks libsndfile and returns `None` when
there is no libsndfile to ask: the duration gate is the one check here that is
silently absent in an image without `soundfile`, on the argument that a
missing encoder is a much louder failure two lines later.

`VoiceLibrary.resolve(name)` returns `None` for "the model's own voice", which
is a real request and not an error. An unknown name raises, naming the voices
that *are* installed: returning a different speaker under a 200 is the failure
a caller cannot hear until somebody else does. **The library is a startup
snapshot, not a view of the directory** — `SpeechEngine` builds it once in its
constructor and never rebuilds it, so a voice deleted under a running
deployment stays advertised and answers 500 on use, and one added is invisible
until the next launch.

### The starter library is fetched, and never fails a launch

`ensure_default_voices(directory)` installs three LibriTTS clips
(`mythicinfinity/libritts`, config `clean`, split `dev.clean`) at the pinned
offsets `(0, 400, 900)`, named `libritts-<speaker id>`, with an
`ATTRIBUTION.txt` beside them because the corpus is CC BY 4.0. Four choices in
that sentence are load-bearing:

- **LibriTTS and not LibriSpeech.** LibriSpeech transcripts are uppercase and
  unpunctuated, and the transcript is what the clone conditions on. "MISTER
  QUILTER IS THE APOSTLE" is text nobody has ever said out loud.
- **Pinned offsets, and `_pick_row` takes the first usable row rather than the
  best.** Every node in a cluster then ends up with the same three voices under
  the same names; a `voice` that means a different speaker depending on which
  machine answered is the worst kind of routing bug to debug.
- **Unlovely names.** The instinct is "narrator" or "amelia", and that is a
  small lie about a real LibriVox reader. The speaker id is also the only handle
  that leads back to the corpus row.
- **Every failure is a note, never an exception.** The whole body is wrapped, and
  the return value is a list of strings the caller logs. `main` runs it inside
  the try that prints `FATAL_MARKER`, so an escaping exception would turn a slow
  dataset API into a failed deployment. An existing library is never touched:
  one usable pair in the directory and the function does nothing.

Writes are atomic and transcript-last, because `load_voices` requires the
`.txt` beside a clip — a crash mid-fetch leaves an audio file that function
already skips with a reason, where the reverse order would leave a transcript
pointing at nothing.

### `SpeechEngine`: load everything, then generate one at a time

`load()` allocates the codec eagerly through the checkpoint's own `load_codec`
hook rather than on first request: it is a third of the memory this process
holds, and a deployment reporting READY before allocating it would pass the
health check and then fail the first synthesis with an out-of-memory error
nothing on screen could connect to the launch. It reads `codec_sample_rate`
off the model config and raises when there is none — "a model without a codec
is not one" — and clamps `self.window` down to the checkpoint's own
`max_seq_len`, warning when the recipe asked for more, because the launcher
chose the first and the second is what will actually refuse a long request.
`torch.cuda.set_per_process_memory_fraction(gpu_memory_utilization)` is a real
cap here and not a decoration: this process has no paged KV pool to size, so
the fraction applies to the allocator itself, which is what keeps a TTS
deployment inside the budget the fit gate cleared it for.

`synthesize()` runs on a threadpool worker under `self._gpu_lock` and returns a
1-D float32 array. `DEFAULT_MAX_NEW_TOKENS` is 512 — about 24 seconds at
~21.5 frames/s, and the checkpoint's own `generation_config` default.

### `_patch_reference_text_tag` removes seven garbage tokens

The checkpoint's `processing_arktts.py` prepends `<|speaker:0|>` to every
reference transcript. It mirrors the `<|semantic:N|>` convention exactly — same
brackets, same colon, and the code even guards against double-prepending it —
but the tag was never added to this checkpoint's tokenizer vocabulary:
`tokenizer.encode("<|speaker:0|>", add_special_tokens=False)` comes back as
seven ordinary sub-word ids, where `<|im_start|>`, `<|voice|>` and all 4,096
`<|semantic:N|>` round-trip as one. So every cloned-voice request splices seven
garbage tokens in immediately before the real reference sentence.

The patch is scoped to that one confirmed defect and claims nothing more. An
instrumented A/B through this exact class — `tests/tts_diagnose.py --phase 5/6`,
15+ trials — found that removing the tag does **not** fix the separate,
occasional collapse where generation samples end-of-speech after a handful of
frames (roughly a few percent of calls, worse for some reference clips); if
anything the sample skewed slightly worse without it. That collapse is open.
The patch is duck-typed on `hasattr(processor, "_format_reference_text")`, so a
revision that fixes its own vocabulary is a no-op rather than a crash.

### `Admission` counts requests in flight; it does not queue them

One generation runs at a time whatever the number says. `Admission(limit)`
counts `_in_flight` — incremented by `try_enter()` before the request reaches
the threadpool, decremented by `leave()` in a `finally` — and refuses at
`_in_flight >= limit` with a 503 and `Retry-After: 1`, which the gateway's
parking lot already knows how to hold. An unbounded queue would instead keep
every caller until their client timed out and report nothing about it.

**The limit counts the generating request too, so the number of callers who
may wait is `limit - 1`.** `--max-num-seqs` sets it and defaults to 1, which
means one request generating and the next refused immediately, not one
generating and one waiting. The class docstring reads it the other way round;
the arithmetic in `try_enter` is what runs.

### `parse_speech_request` is pure, so the refusals are testable

Validation is a free function returning a frozen `SpeechRequest` or a
`ValueError` carrying the whole reason, which the route turns into OpenAI's
error envelope via `error_body`. It checks the model name too — this server
holds exactly one checkpoint, and answering for a name it does not serve makes
a misrouted request look like a working one. `speed` is refused outright rather
than approximated: resampling is not time-stretching, it moves the pitch, and a
1.5x that quietly returned a chipmunk would be worse than a sentence saying so.
`_optional_float` and `_optional_int` refuse zero and negatives by name.
The checkpoint's own `ValueError`s — a prompt longer than the window is the
common one — are forwarded verbatim from the route, for the reason every
refusal in this project is: it already names what to change.

### The command line mirrors `vllm serve`, and consumes every flag

`deploy/recipes.py::_serve_command` fills `spec.command_template` from the same
knob table for every runtime, and a knob whose recipe key is absent from the
template is dropped in silence. So
`build_parser` names all of them, and `config_from_args` refuses
`--tensor-parallel-size` or `--pipeline-parallel-size` at anything but 1 with a
`SystemExit` naming the fix — consumed in order to refuse, never accepted and
ignored. This is one process holding one checkpoint; a `--tp 2` accepted here
would serve half a model's worth of nothing on a machine the planner had
already committed, and the refusal lands at startup where sparkrun's liveness
check turns it into a FAILED deployment with the sentence in the log, rather
than at the first request. `--voice-dir` defaults to `DERATE_TTS_VOICE_DIR` and
`--no-default-voices` to `DERATE_TTS_DEFAULT_VOICES=0`.

## `__init__.py`

Twelve lines, all of them docstring, and all of them the isolation rule:
everything in this package runs inside a model container, its dependencies are
not in `requirements.txt`, nothing else in `control_plane/` imports it, and
every module keeps its heavy imports inside the functions that need them. It
has no `import` statement and no `__all__`.

The emptiness is the point, and it is not the point people assume. Importing
this package on a node does **not** fail: only `fastapi`, `starlette` and the
standard library are imported at `tts.py`'s module scope, and `fastapi` is
pinned in `requirements.txt` with `starlette` arriving under it — which is why `tests/test_tts_runtime.py` does a bare
`from control_plane.runtimes import tts` at the top and runs 33 tests in the
ordinary suite with no torch and no GPU. What an `__init__` re-exporting
`SpeechEngine` would cost is that property: the class is defined in the same
module as the deferred `torch`/`transformers` imports today only by discipline,
and a package that advertises a surface is a package the rest of
`control_plane/` will eventually import from. The docstring forbids that, and
an empty `__init__` is how the ban is enforced rather than asserted.

## The seam with `deploy/`, `resolver/` and the gateway

Nothing imports this package. Every seam is a string, a filename or an HTTP
route, and each one has a test holding the two ends together.

- **`deploy/flags.py`** owns the `tts` `RuntimeSpec`: `command_template`
  `_TTS_COMMAND` (`python3 -m control_plane.runtimes.tts ...`),
  `custom_command_prefix` `_TTS_CUSTOM_PREFIX`, `max_seqs_key="max_num_seqs"`,
  `health_path="/health"`, `expert_parallel_arg=None` (no experts, and
  appending a flag the parser does not take fails the launch at argv),
  `shards=False`, and the image `ghcr.io/pizzaman213/derate/tts:latest`
  overridable with `DERATE_TTS_IMAGE`.
- **`sparkrun_runtime="vllm"` is on purpose.** sparkrun's runtime field picks
  the orchestration plugin, and every plugin runs an explicit `command:`
  verbatim. There is no plugin for a runtime sparkrun has never heard of, so
  this borrows the one whose solo path is exactly "run this container with this
  command". What comes with it — `HF_HUB_OFFLINE=1` and the vLLM tuning mounts —
  is correct or inert here.
- **The voice library travels as an env var**, because `env:` is the only
  channel that reaches a launched container: the recipe format has no
  `volumes:` key and the HuggingFace cache is the one writable mount. The spec's
  `cache_env` sends `DERATE_TTS_VOICE_DIR` to
  `/cache/huggingface/derate-runtime-cache/voices` — beside `hub/` and never
  inside it, because `registry/modelcache.py` — it is not in `deploy/`, where
  the rest of this list lives — walks `hub/models--*` and would bill a
  reference clip as weights.
- **`deploy/health.py`** probes `HEALTH_PATHS = ("/health", "/v1/models")`, in
  that order. `build_app` serves both; a runtime answering neither never leaves
  LAUNCHING.
- **`deploy/progress.py`** holds the verbatim copy of `FATAL_MARKER` in
  `_RUNTIME_FATAL`.
- **`resolver/support.py`** holds `TTS_ARCHITECTURES`, the claim about what this
  file can drive.
- **`gateway/openai_api.py`** proxies `POST /v1/audio/speech` and adds
  `GET /v1/audio/voices` — not an OpenAI route, and it exists because this
  server serves one and nothing outside the container could ask it. It answers
  from a local target only; no provider implements it.
- **`docker/tts.Dockerfile`** copies `control_plane/__init__.py` and
  `control_plane/runtimes/` and nothing else, sets `PYTHONPATH=/opt/derate`,
  asserts at build time that libsndfile can write WAV, MP3, FLAC, OGG and RAW,
  and sets no `ENTRYPOINT` — an entrypoint would be a second opinion about how
  to start the server. Its `CMD` is `--help`.
- **`ui/src/api/types.ts`** and **`ui/src/tabs/chat/Clip.tsx`** both cite
  `FORMATS` by name — the first for the response formats the chat composer
  offers, `aac` deliberately absent; the second to match the returned
  `Content-Type` as a prefix.

`tests/test_tts_runtime.py` (33 tests, 471 lines) gates everything about this
runtime that needs no GPU. What it cannot cover, in its own words, is that the
checkpoint loads and speaks; that was verified by running the server against
`Audio8/Audio8-TTS-Preview-0.6b` on a GB10 and playing the result.

## Things that look like details and are not

**The support table is a claim about this file, not about the field.** An
architecture in `TTS_ARCHITECTURES` asserts that somebody ran that checkpoint
through `SpeechEngine` and got audio out. Adding a name because the model card
says TTS produces a deployment that plans, fits, launches, and dies at load
with nothing upstream having had a reason to say no.

**`/v1/models` reports `"modality": "speech"`.** It is the one field a client
cannot guess from the name, and it is the same word the gateway's own
`/v1/models` puts on the wire — `Modality.SPEECH` in
`contracts/modality.py`, whose `ENDPOINT_FOR_MODALITY` maps it to
`/v1/audio/speech`. That is what the gateway's modality guard names when
somebody sends a speech model to a text route.

**The reference clip and the generated speech share one window.** 2048 packed
positions by default, clamped down to the checkpoint's `max_seq_len` at load.
That is why `MAX_REFERENCE_SECONDS` is a refusal at startup rather than a
truncation at request time, and why `--max-model-len` is not a free knob.

**`_fetch_bytes` reads `limit + 1` and refuses at the cap.** It does not trust a
`Content-Length` nobody sent. `MAX_DEFAULT_VOICE_BYTES` is 8 MiB and
`DEFAULT_VOICE_TIMEOUT_S` is 30.0.

**Naming no voice is a valid request.** These models generate in their own voice
with no reference, which is what a caller who just wants speech wants. A
deployment with an empty voice library is fully functional, and
`ensure_default_voices` exists to make it nicer, not to make it work.

## Failure behaviour

- **No voice directory, or an empty one.** `load_voices` returns an empty
  `VoiceLibrary`; a missing directory is a `skipped` note, not an error. The
  server speaks in the checkpoint's own voice.
- **A clip with no transcript, an empty one, an unreadable file, or over 30s.**
  Skipped with the reason — the last two only where `soundfile` imports, since
  `_too_long` returns `None` rather than guessing when it cannot decode — logged at startup, and reported in the `skipped`
  array of `GET /v1/audio/voices` — so a mistyped filename is visible rather
  than a voice that silently is not there.
- **The dataset API is slow, down, or returns nothing usable.**
  `ensure_default_voices` returns notes and the launch proceeds. Nothing about
  a starter library is worth a launch.
- **An unknown `voice`, an unknown `response_format`, `speed != 1.0`, a wrong
  `model`, a non-object body, bad JSON.** 400 with OpenAI's error envelope and
  the whole reason string.
- **`aac`, or `opus` in an image with no scipy.** 400 naming the missing encoder
  or resampler and the formats that would have worked, rather than a generic
  "unsupported".
- **More requests in flight than `--max-num-seqs`.** 503, `Retry-After: 1`,
  with a body saying the server synthesises one at a time. At the default of 1
  that is every request that arrives while another is generating.
- **A `ValueError` from the checkpoint mid-generation.** Forwarded verbatim as a
  400. Anything else is `log.exception` and a 500 carrying the exception text.
- **No `codec_sample_rate`, remote code that answers something else, or
  `--tp`/`--pp` above 1.** The process exits before binding a port, having
  logged `FATAL_MARKER`, and `deploy/progress.py` ends the launch on it instead
  of waiting out the readiness timeout.

## Deliberately not built

**Streaming.** The response is the whole file. A partial waveform is not a
decodable one for any container format this server writes.

**Batching.** The DualAR loop holds a fixed KV cache sized for the batch it was
set up with, and this server does not micro-batch across requests.
`--max-num-seqs` therefore bounds admitted requests, not concurrent
generations, of which there is always exactly one.

**`speed`.** No time-stretch exists here, and resampling to fake one moves the
pitch as well as the rate. The refusal says to change it in the player, which
does it properly.

**Sharding.** `--tensor-parallel-size` and `--pipeline-parallel-size` exist only
to refuse anything but 1, and `deploy/flags.py::sharding_refusal` refuses the
same degrees on `shards=False` before the launch starts, so the launch path and
the runtime cannot disagree about which are legal. Asked before a launch rather
than discovered by one: a single-process runtime handed TP=2 passes the fit gate
— the arithmetic is per-rank, and a sharded model fits more easily, not less —
commits two machines, starts one server, and leaves the deployment in LAUNCHING
until the health timeout with nothing on screen naming the cause.

**A second server.** This package has held exactly one module since it was
created, and the isolation rule in `__init__.py` is the reason the count
matters: every module added here is another thing that must never be imported
from a node.
