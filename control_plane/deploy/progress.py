"""What a launch is doing right now, read off the launcher's own words.

A deployment sits in LAUNCHING from the moment `sparkrun run` is invoked
until the backend answers a health probe, and on a first launch that is
twenty minutes of one unchanging word. It is not one thing: a 24.4 GB container
image is pulled, the weights are fetched onto the node, the runtime reads them
onto the GPU, and then it compiles and captures CUDA graphs before it will
answer anything. Those fail differently, take different lengths of time and
want different patience, and until this module existed the product could only
say "launching" through all four.

**Nothing here invents a number or a sentence.** Every phase below is set by a
literal marker one of two programs printed, and the sentence shown to a person
is that program's own line, passed through. An unrecognized line changes
nothing: the phase stands and the previous sentence stands, because the
alternative -- treating the newest line as the current activity -- puts a
stack-trace fragment or a tokenizer warning on screen captioned as progress.

**Every number here was printed by the program doing the work.** Two of the
four steps count themselves, and both do it with a tqdm bar: the model
downloader ("Fetching 16 files: 19%|...| 3/16 [00:45<03:15]") and the
checkpoint loader ("5/11"). Their fractions are read from those counts and
their estimated time from tqdm's own remaining field -- an estimate made by
the thing being estimated. Nothing is extrapolated: there is no fraction and
no ETA for the image pull, the compile or the graph capture, because none of
them reports a total, and a bar filling at a rate this file made up is read as
an estimate and planned around.

One class of line is not progress at all and is read first rather than last:
the runtime announcing that it is dying. A launch whose engine exits inside a
container that sleeps forever otherwise looks alive to everything else in the
system, and is waited out for the full readiness timeout. `_RUNTIME_FATAL`
below is deliberately four literals long.

Where the markers come from, so a version bump can be checked rather than
guessed:

  sparkrun 0.2.40   two sources, and they disagree -- the package's own
                    format strings (`Pulling image:`, `Ensuring model ...
                    locally`, `Distributing model`) and the step headers a
                    real `sparkrun run --dry-run --no-follow` prints on a
                    non-tty (`[2/6] Building image`, `[5/6] Launching vllm
                    runtime`). Neither set is in the other. Matching only the
                    first would report `preparing` through a whole launch.
  vLLM              read out of the image this project launches,
                    ghcr.io/spark-arena/dgx-vllm-eugr-nightly (vLLM
                    0.28.1rc1.dev462, torch 2.13.0+cu130): `Starting to load
                    model`, `Loading safetensors checkpoint shards`,
                    `Loading weights took`, `Compiling a graph for compile
                    range`, `Capturing CUDA graphs`, `Available KV cache
                    memory`, `init engine (profile, create kv cache, warmup
                    model)`.
  SGLang            NOT verified against an image -- the SGLang container is
                    not on the box this was written on. Its markers are
                    absent rather than guessed, which costs a phase caption
                    and nothing else: the manager still seeds `loading` when
                    the container comes up (see manager._wait_for_ready), so
                    an unrecognised runtime reports one phase less precisely
                    instead of reporting something untrue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The phases a launch goes through, in order. `serving` is not set from a log
#: line -- the health probe decides it, and a runtime that says it is ready is
#: not the same thing as a port that answers.
PHASES: tuple[str, ...] = ("preparing", "downloading", "loading", "starting", "serving")

#: The captions live in the UI (`ui/src/state/launchPhase.ts`), not here. This
#: side owns the vocabulary and the order; the words a person reads are the
#: screen's business, and a second copy of them on the wire is a second copy to
#: keep in step.


def rank(phase: str | None) -> int:
    """Position in the ladder, or -1 for a phase nobody recognises."""
    try:
        return PHASES.index(phase or "")
    except ValueError:
        return -1


@dataclass(frozen=True)
class LaunchProgress:
    """One reading. `status` is verbatim; `fraction` is None unless measured."""

    phase: str
    status: str
    fraction: float | None = None
    #: Seconds remaining, as the program doing the work reported them.
    #:
    #: Never computed here. Both the model downloader and the checkpoint
    #: loader are tqdm bars, and tqdm prints its own estimate in every frame
    #: (`[00:45<03:15, 15.0s/it]`); this reads the second field. An estimate
    #: from the thing measuring itself is worth showing. One this file
    #: extrapolated from a rate it also invented is exactly what the project
    #: refuses -- people plan around it.
    eta_s: float | None = None
    #: The runtime said it died. Only ever set from a marker that vLLM logs
    #: immediately before it stops -- see _RUNTIME_FATAL. A launch can fail
    #: without this (the container itself dying is caught elsewhere); nothing
    #: infers it from a line that merely looks alarming.
    fatal: bool = False
    #: Who said it: "sparkrun" or "runtime" for a line one of them printed,
    #: "derate" for the one sentence we write ourselves -- the handoff, where
    #: the container is up and has not said anything yet (manager.py). The two
    #: programs cover different halves of the window and never overlap:
    #: sparkrun's output stops at the moment it hands off, which is the moment
    #: a container log starts existing.
    source: str = "runtime"

    def as_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "status": self.status,
            "fraction": self.fraction,
            "eta_s": self.eta_s,
            "source": self.source,
            "fatal": self.fatal,
        }


#: `Loading safetensors checkpoint shards:  45% Completed | 5/11 [00:12<...]`
#: The pair is the runtime counting its own shards. The percentage beside it
#: is derived from the same pair and rounded, so the pair is what we read.
_SHARDS_RE = re.compile(r"checkpoint shards.*?(\d+)\s*/\s*(\d+)")

#: `Fetching 16 files:  19%|█▉        | 3/16 [00:45<03:15, 15.0s/it]`
#:
#: huggingface_hub's own bar, printed because sparkrun calls
#: `enable_progress_bars()` before `snapshot_download` (models/download.py).
#: It goes to stderr, which the launch stream merges, and tqdm writes full
#: frames through a pipe -- verified, not assumed.
#:
#: Files, not bytes: this is what the downloader counts, and the line beneath
#: it on screen says "3/16" so the denominator is never a mystery. The
#: per-file bars beside it are deliberately not matched -- one file's percent
#: is not the download's, and taking whichever arrived last would make the bar
#: jump between two different questions.
_FETCHING_RE = re.compile(r"Fetching \d+ files:\s*(\d+)%")

#: tqdm's `[{elapsed}<{remaining}, {rate}]`. The second field is the estimate,
#: in `MM:SS` or `H:MM:SS`, and is `?` before there is enough history for one.
_ETA_RE = re.compile(r"\[\d+:\d+(?::\d+)?<(\d+):(\d+)(?::(\d+))?[,\]]")

# -- the marker tables ----------------------------------------------------
#
# Substrings, except where a name sparkrun interpolates forces a pattern.
# Every one of them is a literal a program prints, and two sources were needed
# to get them right: the strings in sparkrun's package, and the lines a real
# `sparkrun run --dry-run --no-follow` actually put on a non-tty. They are not
# the same set. The step headers a person sees ("[2/6] Building image",
# "[5/6] Launching vllm runtime") never appear as literals in the source, and
# the detailed lines from the source ("Pulling image: ...", "Ensuring model
# ... is available locally") are printed underneath them by code paths a dry
# run skips. Both belong here: a launch prints the headers always and the
# detail only when there is work to do, so the headers keep the caption
# honest through the steps that turn out to be instant.

_SPARKRUN_MARKERS: tuple[tuple[str | re.Pattern[str], str], ...] = (
    # The weights, which is the long one and the one worth naming.
    ("Downloading model", "downloading"),
    ("Downloading GGUF model", "downloading"),
    ("Ensuring model", "downloading"),
    ("Distributing model", "downloading"),
    # huggingface_hub's own progress bar, which is where the only honest
    # estimate of when a download ends comes from. It arrives on stderr from
    # inside `sparkrun run`, so it is only readable at all because the launch
    # output is streamed rather than captured.
    (_FETCHING_RE, "downloading"),
    # The container image: 24.4 GB on a cold node (`docker images` on the
    # shipped tag), and indistinguishable from a
    # hang until it was named.
    ("Ensuring container image", "preparing"),
    ("Pulling image", "preparing"),
    ("Distributing image", "preparing"),
    ("Building image", "preparing"),
    ("Distributing resources", "preparing"),
    ("Distributing tuning configs", "preparing"),
    ("Syncing tuning configs", "preparing"),
    # `[1/6] Preparing`, which is a step header and not a sentence, so it is
    # anchored rather than matched anywhere in a line.
    (re.compile(r"^\[\d+/\d+\] Preparing"), "preparing"),
    # Handing off: everything sparkrun does before the runtime opens the
    # checkpoint is finished, so this is the load starting, not more
    # preparation. The container log takes over within seconds either way --
    # `_wait_for_ready` says the same thing the moment the call returns -- but
    # a caption that waits for the first log read spends those seconds naming
    # the step that just ended.
    ("Launching container", "loading"),
    (re.compile(r"Launching \w+ runtime"), "loading"),
    # A sub-step of "Launching <runtime> runtime" and printed after it. Not
    # loading in any literal sense, but the phase only moves forwards, so
    # calling it preparation would freeze the caption on the previous line
    # for as long as it takes -- and what it is really doing is on screen
    # underneath, in its own words.
    ("Detecting InfiniBand", "loading"),
    ("Executing serve command", "loading"),
    ("Launching Ray", "loading"),
    ("Starting head node serve", "loading"),
    ("Starting worker nodes", "loading"),
)

_RUNTIME_MARKERS: tuple[tuple[str | re.Pattern[str], str], ...] = (
    ("Loading safetensors checkpoint shards", "loading"),
    ("Loading pt checkpoint shards", "loading"),
    ("Starting to load model", "loading"),
    # Announces that loading FINISHED, so it belongs to what comes next. The
    # engine work after it -- compile, capture, KV profile -- is minutes on a
    # cold cache and was the least visible part of the whole launch.
    ("Loading weights took", "starting"),
    ("Compiling a graph for compile range", "starting"),
    ("torch.compile", "starting"),
    ("Capturing CUDA graphs", "starting"),
    ("Capturing cudagraphs", "starting"),
    ("Available KV cache memory", "starting"),
    ("init engine", "starting"),
    ("Application startup complete", "starting"),
    ("Uvicorn running on", "starting"),
)

#: The runtime announcing its own death, which is not the same event as the
#: container dying and is the one the manager could not see.
#:
#: A solo launch runs the serve command inside a container that sleeps
#: forever, so when the engine exits the container stays up: `check-job` says
#: the workload is running, the port never answers, and the launch waits out
#: the full READY_TIMEOUT_S. On the box this was written on that is a launch
#: that died after thirty seconds and was watched for thirty minutes, with the
#: reason -- vLLM refusing to start because 49.56 of 121.69 GiB were free
#: against a 0.9 utilization target -- sitting in a log file nothing read.
#:
#: The first three are verified terminal in the shipped image: the first two
#: are logged in `run_engine_core`'s except block, one line above `raise e`,
#: and the third is the RuntimeError the API server raises when the core
#: process is gone. The fourth is not a message but the CLI entrypoint's own
#: frame (`sys.exit(main())` in `/usr/local/bin/vllm`): a config error caught
#: nowhere else unwinds the traceback all the way back to it, which happens
#: only when nothing downstream handled the exception and the interpreter is
#: about to run its default excepthook and exit -- true regardless of which
#: exception it was. It is what a launch whose `max_model_len` exceeded the
#: model's own `max_position_embeddings` printed: pydantic rejected the config
#: inside `create_engine_config`, before `EngineCore` ever forked, so none of
#: the first three ever fired and the launch waited out the full timeout
#: watching a container that was never going to answer.
#:
#: The fifth is ours, and it is here for the same reason the other four are
#: rather than by analogy with them. `control_plane/runtimes/tts.py` does the
#: whole of its parse-load-build before `uvicorn.run`, so a checkpoint whose
#: remote code does not answer the three calls, a config with no codec, or a
#: `--tp 2` the server refuses all exit the process with no port ever bound --
#: the exact shape of failure this tuple exists to catch, in the one runtime
#: this repository writes and therefore the one that had no marker. It is a
#: verbatim copy of `runtimes.tts.FATAL_MARKER`; a copy rather than an import
#: because nothing outside a model container may import that module (torch and
#: transformers are its dependencies), and `tests/test_tts_runtime.py` asserts
#: the two strings match so the copy cannot drift.
#:
#: Nothing else belongs in this tuple. A marker here ends a launch, so "looks
#: like an error" is not the bar -- "this line means the process is on its way
#: out" is.
_RUNTIME_FATAL: tuple[str, ...] = (
    "EngineCore failed to start.",
    "EngineCore encountered a fatal error.",
    "Engine core initialization failed.",
    'File "/usr/local/bin/vllm"',
    "fatal: the speech server failed to start.",
)



def _lines(text: str) -> list[str]:
    """Every line, newest last.

    Split on carriage returns as well as newlines: the shard loader is a tqdm
    bar, so its frames arrive as `\\r`-separated segments of one long line and
    only the last segment is the current one.
    """
    out: list[str] = []
    for chunk in text.replace("\r", "\n").split("\n"):
        stripped = chunk.strip()
        if stripped:
            out.append(stripped)
    return out


def _fraction(line: str) -> float | None:
    """How far through, when the program printed a count of its own work."""
    match = _SHARDS_RE.search(line)
    if match:
        done, total = int(match.group(1)), int(match.group(2))
        return max(0.0, min(1.0, done / total)) if total > 0 else None
    match = _FETCHING_RE.search(line)
    if match:
        return max(0.0, min(1.0, int(match.group(1)) / 100.0))
    return None


def _eta(line: str) -> float | None:
    """Seconds remaining, if a tqdm bar on this line said so.

    `MM:SS` or `H:MM:SS`. A bar with nothing to go on prints `?`, which does
    not match and is therefore reported as no estimate rather than as zero.
    """
    match = _ETA_RE.search(line)
    if not match:
        return None
    parts = [int(p) for p in match.groups() if p is not None]
    if len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        hours, (minutes, seconds) = 0, parts
    return float(hours * 3600 + minutes * 60 + seconds)


def _matches(marker: str | re.Pattern[str], line: str) -> bool:
    return bool(marker.search(line)) if isinstance(marker, re.Pattern) else marker in line


def _classify(
    text: str, markers: tuple[tuple[str | re.Pattern[str], str], ...], source: str
) -> LaunchProgress | None:
    """The newest line that matches a marker, or None if none does."""
    for line in reversed(_lines(text)):
        for marker, phase in markers:
            if _matches(marker, line):
                return LaunchProgress(
                    phase=phase,
                    status=line,
                    fraction=_fraction(line),
                    eta_s=_eta(line),
                    source=source,
                )
    return None


def from_launcher(text: str) -> LaunchProgress | None:
    """Read sparkrun's own output: the image, the weights, the handoff."""
    return _classify(text, _SPARKRUN_MARKERS, "sparkrun")


def from_runtime_log(text: str) -> LaunchProgress | None:
    """Read the container log: the load onto the GPU and the engine start.

    A line saying the engine has died wins over any progress line in the same
    text, wherever it sits in it. Progress markers are read newest-first
    because the newest is what is happening; a death is not a step, it is the
    end, and a shard count printed after it does not undo it.
    """
    for line in _lines(text):
        for marker in _RUNTIME_FATAL:
            if marker in line:
                return LaunchProgress(
                    phase="starting", status=line, source="runtime", fatal=True
                )
    return _classify(text, _RUNTIME_MARKERS, "runtime")


def advance(
    current: LaunchProgress | None, reading: LaunchProgress | None
) -> LaunchProgress | None:
    """Fold a new reading into what we already had, forwards only.

    A log tail is a window, not a stream: a slow poll can land after the
    interesting lines have scrolled out of it and come back with an older
    marker than the one before. Left alone that walks the phase backwards, and
    a stepper that un-ticks a step reads as something going wrong -- the same
    rule the setup screen already applied to its own inference.

    Within one phase the newest sentence always wins, because that is the
    sentence moving: "5/11" must be allowed to replace "3/11".
    """
    if reading is None:
        return current
    if current is None:
        return reading
    # A death is never held back by the ladder. It is the one reading that
    # says the launch is over rather than where it has got to.
    if reading.fatal:
        return reading
    if rank(reading.phase) < rank(current.phase):
        return current
    return reading
