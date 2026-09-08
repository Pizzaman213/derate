#!/usr/bin/env python3
"""A load tester for the derate gateway, with a curses TUI.

Points at the one HTTP surface, asks `/v1/models` what is being served, and
then hammers either one model or every model it named. It routes by the
modality the gateway reports -- text to /v1/chat/completions, embeddings to
/v1/embeddings, speech to /v1/audio/speech, transcription to
/v1/audio/transcriptions -- because "all the models" on a mixed cluster is not
one endpoint.

    python3 loadtest.py                                  # TUI on localhost:8088
    python3 loadtest.py --list
    python3 loadtest.py --model qwen2.5:0.5b -c 16
    python3 loadtest.py --all --no-tui --duration 30      # free targets only
    python3 loadtest.py --base http://spark-01:8088 -c 64 --no-stream
    python3 loadtest.py --all --hammer                    # as hard as it goes

There are two ways to push, and they measure different things.

**Closed loop** (the default) is `--concurrency` slots per model, each holding
one request and sending the next when that one lands. In-flight can never
exceed the number of slots, so the send rate is whatever the server's latency
allows: this answers "how does it behave at N concurrent users", and it cannot
overload anything.

**Open loop** (`--hammer`, or any explicit `--rps`) issues on a schedule
computed from the start of the run and does not wait for anything to come
back. In-flight climbs until the target stops keeping up, which is the point:
this answers "where does it break". `--hammer` also ramps -- doubling the
arrival rate every rung until a bar trips -- and reports the last rung that
held, so the number you get is the knee rather than a number you guessed.

Either way there is no shared rotation. Every model gets its own independent
issuer, so a target that has quietly stopped answering can only ever hold its
own requests: it cannot swallow a pool and report zero requests per second for
the model that was answering fine.

Requests are measured against three clocks, because with an open loop one
number is a lie:

    latency      from when the request was *due* -- what a queued user waits
    service      from when it was actually sent -- what the server took
    send delay   the gap -- how far behind its own schedule the harness fell

A run that reports flat latency while the queue explodes is one that started
its clock at send time. This one does not.

Models that cost money are left out unless --include-paid is passed: a
sustained load test against a metered API is a bill, and the router spills
local -> remote precisely when load saturates the local targets. "Costs money"
is read from /api/providers -- a price, a budget, or a credential -- not from
whether a target is remote, because a box on the LAN running Ollama is remote
and free.

Standard library plus httpx, which is already a runtime dependency. Nothing
here imports control_plane: it is a client of the published API, so it also
works pointed at a coordinator on another box.
"""

from __future__ import annotations

import argparse
import asyncio
import curses
import json
import locale
import os
import struct
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field

import httpx

DEFAULT_BASE = os.environ.get("DERATE_BASE_URL", "http://localhost:8088")

#: One clock, everywhere. Every latency here is the difference between two
#: readings of it, and mixing perf_counter with monotonic makes those
#: differences quietly meaningless.
clock = time.perf_counter

#: Mirrors gateway/errors.py::MAX_DETAIL_CHARS. A body that is not one of the
#: gateway's own JSON errors -- an HTML 502 from something sitting in front of
#: it -- is a diagnostic, not a document.
MAX_RAW_ERROR_CHARS = 500

#: The endpoint each modality is served on. The same table as
#: control_plane/contracts/modality.py, restated rather than imported so this
#: file stays a standalone client of the public API.
ENDPOINT_FOR_MODALITY = {
    "text": "/v1/chat/completions",
    "embedding": "/v1/embeddings",
    "speech": "/v1/audio/speech",
    "transcription": "/v1/audio/transcriptions",
}

#: Every modality the gateway routes. Transcription is the odd one: it is the
#: only upload here, and it used to be listed and skipped because there was
#: "nothing honest to synthesise" for it.
#:
#: That was the wrong test. It reasoned about transcript accuracy, which a
#: load test does not measure. Whisper pads or truncates every clip to a fixed
#: thirty-second mel window before the encoder sees it, so the work a
#: transcription request costs is set by the window and not by what was said:
#: a synthesised clip and a recorded one of the same length are the same load,
#: exactly. What silence cannot tell you is whether the words come back, and
#: that is a different question, asked by the round trip in
#: tests/test_gateway.py rather than by a throughput harness.
#:
#: --audio still exists for when the audio should be real.
DRIVABLE = ("text", "embedding", "speech", "transcription")

SPARK_CHARS = "▁▂▃▄▅▆▇█"

#: Mixed into every prompt so no two requests share a prefix, and so a rerun
#: does not reuse the last run's. The cache on the other end does not restart
#: when this process does.
BOOT_NONCE = os.urandom(4).hex()

# --------------------------------------------------------------------------
# the bars a rung has to clear
#
# The same numbers as tests/load/report.py:9-12, restated rather than imported
# for the reason ENDPOINT_FOR_MODALITY is restated: this file talks to the
# published API and to nothing else in the tree. If they move there, move them
# here, because two harnesses disagreeing about what "broken" means is worse
# than either bar being slightly wrong.

ERROR_RATE_MAX = 0.02
KEEPUP_MIN = 0.90
TAIL_BLOWUP_FACTOR = 10.0
LOOP_LAG_P99_MAX_S = 0.250

#: Requests issued per wakeup at most. A stall that lasts a second must not
#: turn into a second's worth of arrivals fired in one batch -- that is a
#: burst the schedule never asked for, and it lands as a latency spike the
#: target did not cause. tests/load/driver.py:355 uses the same cap.
BURST_CAP = 512

#: The rule of thumb, and it is only that: English averages near four
#: characters per token, so --prompt-tokens 8000 is "about 8000", not 8000.
CHARS_PER_TOKEN = 4

#: Under --hammer with a known context length, fill this much of it. The rest
#: is left for the completion; see max_tokens_for().
HAMMER_CTX_FRACTION = 0.5
HAMMER_PROMPT_MIN = 256
HAMMER_PROMPT_MAX = 8192
#: What --hammer sends when the context length is unknown, which is common:
#: an Ollama provider reports context_length 0 for everything it serves.
HAMMER_PROMPT_FALLBACK = 2048

#: Speech is not made heavier by a longer script. A TTS server synthesises in
#: real time, so a 2000-token input is a ninety-second request that measures
#: patience; the pressure there comes from arrival rate, not from length.
SPEECH_PROMPT_MAX_CHARS = 400

#: The synthesised upload. Thirty seconds because that is Whisper's window:
#: a shorter clip is padded to it and a longer one is split into several, so
#: this is the one length where the request costs exactly one window's work
#: and the number on the screen is per-window rather than per-file.
AUDIO_SECONDS = 30.0
#: 16 kHz mono, which is what every Whisper-family encoder resamples to. Send
#: it at that rate and the server's resampler does nothing, so the harness is
#: not measuring its own choice of sample rate.
AUDIO_RATE = 16000

#: gateway/settings.py::GatewaySettings.max_audio_upload_bytes. Restated for
#: the reason ENDPOINT_FOR_MODALITY above is restated -- this file imports
#: nothing from control_plane -- and used to refuse an oversized --audio here,
#: where the message can name the file, rather than after a run has started
#: and every request comes back 413.
MAX_AUDIO_UPLOAD_BYTES = 25 * 1024 * 1024

#: What to call a --audio file on the wire. The server decodes by content, not
#: by this, but a wrong one is the kind of thing that gets blamed on the
#: server -- and OpenAI's own endpoint rejects an unknown extension outright.
AUDIO_TYPES = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".opus": "audio/opus", ".m4a": "audio/mp4",
    ".mp4": "audio/mp4", ".webm": "audio/webm", ".mpga": "audio/mpeg",
    ".mpeg": "audio/mpeg",
}


# --------------------------------------------------------------------------
# the world


@dataclass(frozen=True)
class Model:
    id: str
    modality: str
    context_length: int | None
    kinds: tuple[str, ...]
    targets: int
    #: Provider ids serving this name, and the subset of them that charge.
    #: Both empty when /api/providers could not be read -- see priced_known.
    providers: tuple[str, ...] = ()
    charged_by: tuple[str, ...] = ()
    #: False when the pricing read failed, which switches `metered` back to
    #: the coarse rule. Never assume free.
    priced_known: bool = True

    @property
    def drivable(self) -> bool:
        return self.modality in DRIVABLE

    @property
    def metered(self) -> bool:
        """Whether load sent here can leave the building and cost money.

        Any charging provider counts, not just an all-remote model: the router
        is LOCAL_FIRST on a mixed name, which means it spills to the paid
        upstream exactly when the local targets are saturated -- which is what
        a load test is for. A model that bills only under load is the one worth
        being careful about.

        Remote is not the test. A Raspberry Pi on the LAN running Ollama is a
        remote target and costs nothing; OpenRouter is a remote target and
        costs money. Only the second one should stop a run.
        """
        if not self.priced_known:
            # No pricing to read. Fall back to the coarse rule and overcount:
            # refusing a free target is an inconvenience, hammering a metered
            # one by accident is a bill.
            return "remote" in self.kinds
        return bool(self.charged_by)

    @property
    def why_metered(self) -> str:
        if not self.metered:
            return ""
        if not self.priced_known:
            return "remote, and the provider pricing could not be read"
        return "billed by " + ", ".join(self.charged_by)

    @property
    def why_not(self) -> str:
        if self.drivable:
            return ""
        # A modality this harness has never heard of. Not a skip to be quietly
        # fixed by adding it to DRIVABLE: nothing here knows what to send, and
        # guessing an endpoint would measure a 404.
        return f"unknown modality {self.modality!r}"


def classify_providers(rows) -> tuple[dict[str, set], dict[str, set]]:
    """(served_name -> provider ids, served_name -> the ones that charge).

    A provider charges when it prices a model, carries a spend budget, or
    needs a credential. The last one is a proxy rather than a fact -- a keyed
    self-hosted runtime is free and would be counted -- but it errs towards
    refusing to spend money without being told to, and `--include-paid` and
    `--exclude-provider` are there for when it guesses wrong. Whatever it
    decides, it says so by name.
    """
    serving: dict[str, set] = {}
    charging: dict[str, set] = {}
    for row in rows or ():
        pid = row.get("provider_id") or row.get("kind") or "?"
        key_state = row.get("key_state")
        pays = bool(
            row.get("daily_budget_usd") is not None
            or row.get("api_key_ref")
            or (key_state and key_state != "not_needed")
        )
        for entry in row.get("models") or ():
            name = entry.get("served_name")
            if not name:
                continue
            serving.setdefault(name, set()).add(pid)
            priced = any(
                isinstance(entry.get(k), (int, float)) and entry[k]
                for k in ("input_cost_per_mtok", "output_cost_per_mtok")
            )
            if pays or priced:
                charging.setdefault(name, set()).add(pid)
    return serving, charging


async def discover(base: str, headers: dict, timeout: float) -> tuple[list[Model], list[str]]:
    """What is being served, and what any of it costs. Plus what went wrong.

    /v1/models is the OpenAI surface and carries no pricing, so the second
    half comes from /api/providers. That read is allowed to fail -- the base
    URL may be a coordinator that does not expose it, or one that wants
    credentials this run does not have -- and when it does, every model falls
    back to the coarse remote-is-paid rule and the caller is told why.
    """
    root = base.rstrip("/")
    notes: list[str] = []
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        resp = await client.get(root + "/v1/models")
        resp.raise_for_status()
        body = resp.json()
        serving: dict[str, set] = {}
        charging: dict[str, set] = {}
        priced_known = True
        try:
            providers = await client.get(root + "/api/providers")
            providers.raise_for_status()
            serving, charging = classify_providers(providers.json())
        except Exception as exc:
            priced_known = False
            notes.append(
                f"could not read {root}/api/providers ({type(exc).__name__}), so "
                f"nothing here knows what anything costs: falling back to "
                f"treating every remote target as paid."
            )

    out = []
    for row in body.get("data", []):
        name = row["id"]
        out.append(
            Model(
                id=name,
                modality=row.get("modality") or "text",
                context_length=row.get("context_length"),
                kinds=tuple(row.get("target_kinds") or ()),
                targets=int(row.get("target_count") or 0),
                providers=tuple(sorted(serving.get(name, ()))),
                charged_by=tuple(sorted(charging.get(name, ()))),
                priced_known=priced_known,
            )
        )
    return out, notes


# --------------------------------------------------------------------------
# what to send


FILLER = (
    "cluster interconnect bandwidth tensor parallel pipeline stage kv cache "
    "prefill decode scheduler admission control token budget throughput "
    "latency percentile saturation backpressure replica placement topology "
).split()


def synth_prompt(tokens: int, nonce: str, tail: str) -> str:
    """A prompt of roughly `tokens` tokens, unique from its first character.

    The nonce goes first and nowhere else. Prefix caches hash blocks in order,
    so changing the opening token invalidates every block behind it -- which
    is the entire point. A load test that sends the same prompt every time
    with temperature 0 is measuring the cache, and reports a throughput figure
    the model never produced.

    Length is approximate by construction: see CHARS_PER_TOKEN.
    """
    head = f"#{nonce} "
    if tokens <= 0:
        return head + tail
    target = tokens * CHARS_PER_TOKEN
    words: list[str] = []
    size = len(head) + len(tail) + 1
    i = 0
    while size < target:
        word = FILLER[i % len(FILLER)]
        words.append(word)
        size += len(word) + 1
        i += 1
    return head + " ".join(words) + " " + tail


def prompt_tokens_for(model: Model, args) -> int:
    """How long this model's prompt should be. 0 means "send --prompt as is"."""
    if args.prompt_tokens:
        return args.prompt_tokens
    if not args.hammer:
        return 0
    ctx = model.context_length or 0
    if ctx > 0:
        return max(HAMMER_PROMPT_MIN,
                   min(HAMMER_PROMPT_MAX, int(ctx * HAMMER_CTX_FRACTION)))
    return HAMMER_PROMPT_FALLBACK


def max_tokens_for(model: Model, args, prompt_tokens: int) -> int:
    """`--max-tokens`, reduced to what the context can still hold.

    Prompt plus completion has to fit, and a run whose every request is
    refused for overflowing the window measures the validator. Ten percent is
    left spare because the token count above is a rule of thumb, not a count.
    """
    ctx = model.context_length or 0
    if ctx <= 0:
        return args.max_tokens
    room = int(ctx * 0.9) - prompt_tokens
    return max(16, min(args.max_tokens, room))


def build_body(model: Model, args, nonce: str) -> dict:
    tokens = prompt_tokens_for(model, args)
    prompt = synth_prompt(tokens, nonce, args.prompt)
    if model.modality == "text":
        body = {
            "model": model.id,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens_for(model, args, tokens),
            "temperature": args.temperature,
        }
        if args.stream:
            body["stream"] = True
        return body
    if model.modality == "embedding":
        return {"model": model.id, "input": prompt}
    if model.modality == "speech":
        return {
            "model": model.id,
            "input": prompt[:SPEECH_PROMPT_MAX_CHARS],
            "voice": args.voice,
        }
    if model.modality == "transcription":
        # Not a JSON body at all. See build_upload.
        raise ValueError("a transcription request is a multipart upload")
    raise ValueError(f"nothing to send to a {model.modality} model")


def synth_wav(seconds: float, rate: int, nonce: str) -> bytes:
    """A real mono 16-bit WAV of `seconds`, built from the standard library.

    Written by hand rather than encoded, for the reason everything else in
    this file is: it imports nothing from control_plane and nothing from PyPI
    but httpx, and a load harness that needs libsndfile installed before it
    can measure a transcription server is one nobody runs. The header layout
    is the same one tests/test_tts_runtime.py::wav writes; restated, not
    imported, on the same rule as ENDPOINT_FOR_MODALITY.

    The samples are not silence -- the nonce is expanded into them -- but it
    is the *run* that gets a nonce, not the request. That is the one place
    this deliberately does not follow synth_prompt: a prompt is varied per
    request because a prefix cache would otherwise answer it, and the upload
    cannot be, because --audio sends one real file over and over and a
    synthetic path that defeated a cache the real path walks straight into
    would be measuring something no operator can reproduce. Both paths send
    the same bytes every time; across runs, BOOT_NONCE makes them differ, so
    a server that kept yesterday's answer cannot hand it back today.

    It is not speech and does not pretend to be. What it is, exactly, is one
    Whisper window of work.
    """
    frames = max(1, int(seconds * rate))
    seed = int(nonce[-8:] or "0", 16) or 1
    # A cheap LCG rather than `random`, so the bytes are a pure function of
    # the nonce and a rerun with the same one is reproducible.
    samples = bytearray(frames * 2)
    value = seed & 0xFFFFFFFF
    for i in range(frames):
        value = (1103515245 * value + 12345) & 0xFFFFFFFF
        # Low amplitude on purpose: this is filler, and a full-scale square
        # wave through somebody's speakers while they debug is unkind.
        sample = ((value >> 16) & 0x1FFF) - 0x1000
        samples[2 * i] = sample & 0xFF
        samples[2 * i + 1] = (sample >> 8) & 0xFF
    data = bytes(samples)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


def upload_clip(args) -> bytes:
    """The bytes every transcription request in this run uploads.

    Built once and held. Thirty seconds of 16 kHz mono is 480k samples and
    close to a tenth of a second to generate in Python -- per request that is
    the harness spending the event loop on its own filler while requests wait
    to be issued, and it lands in the numbers as send delay and loop lag,
    which is to say as the target's fault.
    """
    clip = getattr(args, "_clip", None)
    if clip is None:
        clip = args.audio_bytes
        if clip is None:
            clip = synth_wav(args.audio_seconds, AUDIO_RATE, BOOT_NONCE)
        args._clip = clip
    return clip


def build_upload(model: Model, args) -> tuple[dict, dict]:
    """(form fields, files) for one transcription request.

    The model arrives as a form field here rather than a JSON key -- the
    gateway reads it back out of the raw multipart body (openai_api.py::
    _multipart_field) and never re-encodes the form, so what is built here is
    what the runtime receives.
    """
    clip = upload_clip(args)
    data = {"model": model.id}
    if args.language:
        data["language"] = args.language
    return data, {"file": (args.audio_name, clip, args.audio_type)}


# --------------------------------------------------------------------------
# accounting


@dataclass
class Stats:
    sent: int = 0
    ok: int = 0
    err: int = 0
    #: Arrivals the schedule called for that were never sent, because this
    #: model already had --max-inflight requests outstanding. Counted and not
    #: queued: a backlog held in the harness is a backlog hidden from the
    #: person reading the screen.
    dropped: int = 0
    inflight: int = 0
    peak_inflight: int = 0
    tokens: int = 0
    nbytes: int = 0
    cost: float = 0.0
    #: From when the request was due. The one a queued user experiences.
    latency: deque = field(default_factory=lambda: deque(maxlen=20000))
    #: From when it was actually put on the wire. What the server took.
    service: deque = field(default_factory=lambda: deque(maxlen=20000))
    #: The gap between the two. How far behind schedule this process fell.
    send_delay: deque = field(default_factory=lambda: deque(maxlen=20000))
    ttft: deque = field(default_factory=lambda: deque(maxlen=20000))
    finished_at: deque = field(default_factory=lambda: deque(maxlen=20000))
    codes: Counter = field(default_factory=Counter)
    #: When this model was first asked anything and last answered anything.
    #: Both from `clock`; zero means it has not happened yet.
    first_send: float = 0.0
    last_end: float = 0.0
    #: Bumped once per measured request, so the percentile cache below can
    #: tell "nothing new" from "same length, entirely different window".
    seq: int = 0
    _quant: tuple | None = None
    _quant_seq: int = -1
    _quant_at: float = 0.0

    def note_send(self, now: float) -> None:
        if not self.first_send:
            self.first_send = now

    def record_end(self, now: float) -> None:
        self.finished_at.append(now)
        self.last_end = now

    def quantiles(self, now: float, max_age: float = 0.25) -> tuple:
        """(p50, p90, p99, TTFT p50) of latency, recomputed at most every
        `max_age`.

        Sorting the whole window four times per model per frame is the harness
        spending its own CPU on its own display: twenty frames a second over a
        20k sample window is millions of comparisons a second taken from the
        event loop that is supposed to be issuing requests. Cached against
        `seq`, so an idle or stopped run re-sorts nothing at all and a summary
        asking for max_age=0 still sees the last request measured.
        """
        if self._quant is not None and (
            self._quant_seq == self.seq or now - self._quant_at < max_age
        ):
            return self._quant
        ordered = sorted(self.latency)
        first = sorted(self.ttft)
        self._quant = (rank(ordered, 50), rank(ordered, 90), rank(ordered, 99),
                       rank(first, 50))
        self._quant_seq = self.seq
        self._quant_at = now
        return self._quant

    def stalled_for(self, now: float) -> float:
        """Seconds spent with requests in flight and nothing coming back.

        `INFL 8 · OK 0 · ERR 0` is the harness holding still while a target
        says nothing, and on the screen it looks exactly like a slow start.
        Zero when nothing is in flight.
        """
        if not self.inflight:
            return 0.0
        since = self.last_end or self.first_send
        return max(0.0, now - since) if since else 0.0

    @property
    def abandoned(self) -> int:
        return max(0, self.sent - self.dropped - self.ok - self.err)

    def rps(self, now: float, window: float = 5.0) -> float:
        cut = now - window
        while self.finished_at and self.finished_at[0] < cut:
            self.finished_at.popleft()
        if not self.finished_at:
            return 0.0
        span = min(window, max(0.001, now - self.finished_at[0]))
        return len(self.finished_at) / span


def rank(ordered, q: float) -> float | None:
    """Nearest-rank percentile of an already-sorted sequence. No interpolation:
    these are observed latencies, and a p99 that no request actually took is a
    worse number to argue with."""
    if not ordered:
        return None
    k = int(round((q / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, k))]


def pct(values, q: float) -> float | None:
    return rank(sorted(values), q)


def describe_error(code: int, raw: bytes) -> str:
    """The gateway's own sentence, verbatim.

    Its refusals name what to change -- which models do exist, which endpoint
    the modality wanted, what state a deployment is in -- so they are surfaced
    whole rather than reduced to a status code.
    """
    try:
        body = json.loads(raw)
        message = body["error"]["message"]
        if isinstance(message, str) and message:
            return message
    except Exception:
        pass
    text = raw.decode("utf-8", "replace").strip().replace("\n", " ")
    return text[:MAX_RAW_ERROR_CHARS] or f"HTTP {code} with an empty body."


# --------------------------------------------------------------------------
# rungs


@dataclass
class Rung:
    """One step of the ramp for one model: an offered rate, held for a window,
    then drained and judged. Closed-loop runs have exactly one, never judged.
    """

    index: int
    rate: float
    started: float
    ended: float = 0.0
    sent: int = 0
    dropped: int = 0
    ok: int = 0
    err: int = 0
    latency: list = field(default_factory=list)
    service: list = field(default_factory=list)
    send_delay: list = field(default_factory=list)
    left_inflight: int = 0
    lag_p99: float = 0.0

    @property
    def span(self) -> float:
        return max(1e-6, (self.ended or clock()) - self.started)

    @property
    def done(self) -> int:
        return self.ok + self.err

    @property
    def achieved(self) -> float:
        return self.done / self.span

    @property
    def err_rate(self) -> float:
        return self.err / self.done if self.done else 0.0


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    #: True when what broke was this process, not the thing being measured.
    #: The distinction is the difference between a useful number and a lie.
    harness: bool = False


def judge(rung: Rung, baseline_p50: float | None, args) -> Verdict:
    """Did this rung hold? The bars are the ones at the top of this file.

    Loop lag is checked first and on purpose. A harness that cannot keep its
    own schedule produces every symptom of a saturated server -- rising
    latency, falling throughput, a growing backlog -- and writing that down as
    the cluster's ceiling is the single easiest way to publish a wrong number.
    """
    if rung.lag_p99 > LOOP_LAG_P99_MAX_S:
        return Verdict(
            False,
            f"this process fell {rung.lag_p99 * 1000:.0f}ms behind its own event "
            f"loop, so the ceiling found here is the harness's, not the target's",
            harness=True,
        )
    if rung.sent and not rung.done:
        return Verdict(False, f"{rung.sent} requests sent, nothing came back")
    if rung.err_rate > args.ramp_err:
        return Verdict(
            False,
            f"{rung.err_rate * 100:.1f}% errors, over the {args.ramp_err * 100:g}% bar",
        )
    if rung.dropped:
        return Verdict(
            False,
            f"{rung.dropped} arrivals dropped: {args.max_inflight} requests were "
            f"already outstanding, so the target is not keeping up with the schedule",
        )
    if rung.left_inflight:
        return Verdict(
            False,
            f"{rung.left_inflight} still in flight after {args.ramp_settle:g}s of "
            f"quiet -- the backlog outlived the rung that made it",
        )
    p99 = pct(rung.latency, 99)
    if baseline_p50 and p99 and p99 > TAIL_BLOWUP_FACTOR * baseline_p50:
        return Verdict(
            False,
            f"p99 {fmt_secs(p99)} is over {TAIL_BLOWUP_FACTOR:g}x the first rung's "
            f"p50 of {fmt_secs(baseline_p50)}",
        )
    if rung.rate and rung.achieved < KEEPUP_MIN * rung.rate:
        return Verdict(
            False,
            f"offered {rung.rate:.1f}/s and only {rung.achieved:.1f}/s came back",
        )
    return Verdict(True)


# --------------------------------------------------------------------------
# the router's own turn-taking


class PolicyPin:
    """Take the router off whatever it is on, for the duration of the run.

    A round robin hands each request to the next target in turn, which is the
    opposite of what a saturation test asks. The question is how much load a
    path carries before it breaks, and taking turns spreads the answer across
    targets that were never the subject. `least_outstanding` sends each
    request to whichever target is least busy, so pressure lands where there
    is room and the number at the end belongs to the cluster rather than to
    the rotation.

    Restoring is the awkward half, and it is why DELETE /api/routing/{model}
    exists. PUT is not its own inverse: re-PUTting the policy a model resolved
    to registers an override where there was none, which pins the model on a
    policy the auto ladder would otherwise have moved it off. So a model that
    was on auto is put back with DELETE, and a gateway too old to have that
    route is told about it rather than quietly left pinned.
    """

    def __init__(self, base: str, headers: dict, timeout: float, policy: str,
                 transport=None):
        self.base = base.rstrip("/")
        self.headers = headers
        self.timeout = timeout
        self.policy = policy
        #: Only ever set by the tests, which drive a real gateway app in
        #: process. Restoring wrong rewrites somebody's routing config without
        #: saying so, which is not a thing to leave unexercised.
        self.transport = transport
        #: served_name -> (policy it was on, whether that was auto-selected)
        self.saved: dict[str, tuple[str, bool]] = {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout, headers=self.headers, transport=self.transport
        )

    async def apply(self, names) -> list[str]:
        notes: list[str] = []
        async with self._client() as client:
            try:
                resp = await client.get(self.base + "/api/routing")
                resp.raise_for_status()
                configs = {row["served_name"]: row for row in resp.json()}
            except Exception as exc:
                return [
                    f"could not read /api/routing ({type(exc).__name__}): leaving "
                    f"the routing policy alone, so the router may still be taking turns."
                ]
            for name in names:
                row = configs.get(name)
                if row is None:
                    continue
                was = row.get("policy")
                auto = bool(row.get("auto_selected"))
                if was == self.policy:
                    # Already there. Pinning it would only register an override
                    # that then has to be undone.
                    continue
                try:
                    put = await client.put(
                        f"{self.base}/api/routing/{name}", json={"policy": self.policy}
                    )
                    put.raise_for_status()
                except Exception as exc:
                    notes.append(
                        f"{name}: could not pin the policy ({type(exc).__name__}); "
                        f"it stays on {was}"
                    )
                    continue
                self.saved[name] = (was, auto)
                notes.append(
                    f"{name}: {was} -> {self.policy}" + (" (was auto)" if auto else "")
                )
        return notes

    async def restore(self) -> list[str]:
        if not self.saved:
            return []
        notes: list[str] = []
        async with self._client() as client:
            for name, (was, auto) in self.saved.items():
                try:
                    if auto:
                        resp = await client.delete(f"{self.base}/api/routing/{name}")
                        if resp.status_code == 405:
                            # No DELETE on this build. Say what is being left
                            # behind rather than pretending it was put back.
                            await client.put(
                                f"{self.base}/api/routing/{name}", json={"policy": was}
                            )
                            notes.append(
                                f"{name}: back on {was}, but this gateway has no "
                                f"DELETE /api/routing/{{model}}, so it is now an "
                                f"explicit override where it used to be automatic"
                            )
                            continue
                        resp.raise_for_status()
                        notes.append(f"{name}: back to auto ({was})")
                    else:
                        resp = await client.put(
                            f"{self.base}/api/routing/{name}", json={"policy": was}
                        )
                        resp.raise_for_status()
                        notes.append(f"{name}: back on {was}")
                except Exception as exc:
                    notes.append(
                        f"{name}: COULD NOT RESTORE the routing policy "
                        f"({type(exc).__name__}) -- it is still pinned to {self.policy}"
                    )
        self.saved.clear()
        return notes


# --------------------------------------------------------------------------
# the driver


class Runner:
    def __init__(self, args, models: list[Model]):
        self.args = args
        self.models = models
        #: Open loop issues on a schedule and never waits; closed loop holds a
        #: fixed number of requests and sends the next when one lands.
        self.open_loop = bool(args.hammer or args.rps)
        self.ramping = bool(args.hammer and args.ramp and not args.rps)
        self.concurrency = max(1, args.concurrency)
        self.stats: dict[str, Stats] = {m.id: Stats() for m in models}
        self.total = Stats()
        #: (model id, status code, verbatim message) -> count
        self.errors: Counter = Counter()
        self.running = False
        self.stop_reason = ""
        self.client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task] = []
        #: Open-loop requests are fired and not awaited, so something has to
        #: hold a reference or the loop will garbage-collect them mid-flight.
        self._inflight: set[asyncio.Task] = set()
        #: Closed loop only. The slot numbers currently staffed, per model: a
        #: slot's number is what decides whether it is surplus, so the pool is
        #: topped up by number rather than by count.
        self._live: dict[str, set] = {m.id: set() for m in models}
        self._sent = 0
        self._nonce = 0
        #: How late this process is waking up, in seconds. A generator that is
        #: itself starved reports its own ceiling as if it were the target's.
        self.loop_lag = 0.0
        self.lag_samples: deque = deque(maxlen=600)
        self._accum = 0.0
        self._since = 0.0
        self.rps_history: deque = deque(maxlen=120)
        self._last_sample = 0.0

        # -- ramp state, per model, because the issuers are independent -----
        start_rate = float(args.rps or args.ramp_start)
        self.rate: dict[str, float] = {m.id: start_rate for m in models}
        self.rungs: dict[str, list[Rung]] = {m.id: [] for m in models}
        self.current: dict[str, Rung | None] = {m.id: None for m in models}
        self.verdicts: dict[str, list[Verdict]] = {m.id: [] for m in models}
        self.knee: dict[str, Rung | None] = {m.id: None for m in models}
        self.baseline: dict[str, float | None] = {m.id: None for m in models}
        self.phase: dict[str, str] = {m.id: "idle" for m in models}

    # -- lifecycle ---------------------------------------------------------

    @property
    def elapsed(self) -> float:
        if self.running:
            return self._accum + (clock() - self._since)
        return self._accum

    def ensure_client(self) -> None:
        if self.client is not None:
            return
        # The pool is left unbounded on purpose: this runner's own ceiling is
        # the limiter, and httpx's default cap of 100 connections would
        # silently become the thing under test at high concurrency.
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0, read=self.args.timeout, write=self.args.timeout, pool=None
            ),
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=None),
            headers=self.args.headers,
        )

    def start(self) -> None:
        if self.running or not self.models:
            return
        self.ensure_client()
        self.running = True
        self.stop_reason = ""
        self._since = clock()
        if self.open_loop:
            for model in self.models:
                self.phase[model.id] = "running"
                self._tasks.append(asyncio.create_task(self._drive_open(model)))
        else:
            self.fill()

    def stop(self, reason: str = "stopped") -> None:
        if not self.running:
            return
        self._accum += clock() - self._since
        self.running = False
        self.stop_reason = reason

    def reset(self) -> None:
        self.stats = {m.id: Stats() for m in self.models}
        self.total = Stats()
        self.errors = Counter()
        self.rps_history.clear()
        self.lag_samples.clear()
        self.loop_lag = 0.0
        self._sent = 0
        self._accum = 0.0
        self._since = clock()
        start_rate = float(self.args.rps or self.args.ramp_start)
        for m in self.models:
            self.rate[m.id] = start_rate
            self.rungs[m.id] = []
            self.current[m.id] = None
            self.verdicts[m.id] = []
            self.knee[m.id] = None
            self.baseline[m.id] = None

    async def aclose(self) -> None:
        self.stop("stopped")
        for task in list(self._tasks) + list(self._inflight):
            task.cancel()
        pending = list(self._tasks) + list(self._inflight)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._inflight.clear()
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    # -- knobs -------------------------------------------------------------

    def set_concurrency(self, n: int) -> None:
        self.concurrency = max(1, n)
        self.fill()

    def set_rate(self, rate: float) -> str:
        """Pin every issuer to `rate`. Turns the ramp off: a ramp and a hand
        on the dial are two things steering the same number."""
        rate = max(0.1, rate)
        note = ""
        if self.ramping:
            self.ramping = False
            note = "ramp off, rate held by hand"
        for m in self.models:
            self.rate[m.id] = rate
        return note

    def current_rate(self) -> float:
        return max((self.rate[m.id] for m in self.models), default=0.0)

    def shape_note(self) -> str:
        n = len(self.models)
        each = f" each of {n}" if n > 1 else ""
        if not self.open_loop:
            return f"closed loop, {self.concurrency} slots{each}"
        if self.ramping:
            return f"open loop, ramping from {self.args.ramp_start:g}/s{each}"
        return f"open loop, {self.current_rate():.1f}/s{each}"

    def note_loop(self, asked: float, took: float) -> None:
        """Record how much longer than `asked` the event loop took to come
        back. Kept as a window as well as a decayed peak: the peak is what to
        show, the window is what a rung is judged against."""
        over = max(0.0, took - asked)
        self.lag_samples.append(over)
        self.loop_lag = max(over, self.loop_lag * 0.9)

    def lag_p99(self) -> float:
        return pct(self.lag_samples, 99) or 0.0

    def fill(self) -> None:
        """Top the closed-loop slot pools up, one pool per model.

        Shrinking is left to the workers, which retire between requests so a
        shrink never abandons an in-flight one. Slots are numbered and staffed
        by number, so growing and shrinking moves the boundary rather than
        reshuffling which target is being driven by whom.
        """
        if not self.running or self.open_loop:
            return
        self._tasks = [t for t in self._tasks if not t.done()]
        for model in self.models:
            live = self._live[model.id]
            if self.current[model.id] is None:
                rung = Rung(index=0, rate=0.0, started=clock())
                self.current[model.id] = rung
                self.rungs[model.id].append(rung)
            for slot in range(self.concurrency):
                if slot not in live:
                    live.add(slot)
                    self._tasks.append(asyncio.create_task(self._slot(model, slot)))

    def tick(self) -> None:
        """Enforce the stop conditions from somewhere that is not blocked.

        An issuer only reaches its own check between requests, so a target
        that takes 12s to answer would hold a `--duration 6` run open for
        another full request. This runs on the render loop, which never waits
        on a socket, so the deadline means the deadline. Requests already in
        flight are left to finish -- they were issued inside the window.
        """
        if not self.running:
            return
        if self.args.duration and self.elapsed >= self.args.duration:
            self.stop(f"{self.args.duration:g}s elapsed")
        elif self.args.requests and self._sent >= self.args.requests and not self.total.inflight:
            self.stop(f"{self.args.requests} requests")
        elif self.open_loop and all(
            self.phase[m.id] in ("knee", "done") for m in self.models
        ):
            kneed = [m.id for m in self.models if self.phase[m.id] == "knee"]
            self.stop("found the knee" if kneed else "issuers finished")

    def sample(self, now: float) -> None:
        if now - self._last_sample < 1.0:
            return
        self._last_sample = now
        self.rps_history.append(self.total.rps(now))

    def hit_limit(self) -> bool:
        if self.args.requests and self._sent >= self.args.requests:
            self.stop(f"{self.args.requests} requests")
            return True
        if self.args.duration and self.elapsed >= self.args.duration:
            self.stop(f"{self.args.duration:g}s elapsed")
            return True
        return False

    # -- closed loop -------------------------------------------------------

    async def _slot(self, model: Model, slot: int) -> None:
        """One request in flight, forever, against one model.

        A slot belongs to a model and to nothing else. There is no shared
        rotation to take the next target off, which is the whole point: with
        one pool feeding a global round-robin, a target that stops answering
        collects every worker within a single lap, the fast model's row goes
        to zero requests per second with an empty queue, and turning the
        concurrency up just feeds the stall. Here a silent target can only
        ever hold its own.
        """
        mid = model.id
        try:
            while self.running:
                # Retiring here rather than by cancellation: a slot numbered at
                # or above the current concurrency is surplus, and it stands
                # down between requests so nothing in flight is abandoned.
                if slot >= self.concurrency:
                    return
                if self.hit_limit():
                    return
                rung = self.current[mid]
                self.stats[mid].sent += 1
                self.total.sent += 1
                self._sent += 1
                if rung is not None:
                    rung.sent += 1
                self._book(self.stats[mid])
                # Closed loop: a request is due when the previous one landed,
                # so latency and service are the same number by construction
                # and send delay is zero. That is the honest reading -- nothing
                # was waiting in a queue that this process was keeping.
                await self._one(model, clock(), rung)
        finally:
            self._live[mid].discard(slot)

    # -- open loop ---------------------------------------------------------

    async def _drive_open(self, model: Model) -> None:
        """This model's pressure, from the first rung to wherever it breaks."""
        mid = model.id
        index = 0
        try:
            while self.running:
                rung = Rung(index=index, rate=self.rate[mid], started=clock())
                self.rungs[mid].append(rung)
                self.current[mid] = rung
                self.phase[mid] = "running"

                window = self.args.ramp_step if self.ramping else float("inf")
                await self._pump(model, rung, window)
                rung.ended = clock()
                if not self.running:
                    return
                if not self.ramping:
                    # The rate was changed by hand mid-rung. Start a fresh one
                    # against the new schedule rather than carrying a backlog
                    # computed from the old rate.
                    index += 1
                    continue

                self.phase[mid] = "settling"
                await self._settle(model)
                rung.left_inflight = self.stats[mid].inflight
                rung.lag_p99 = self.lag_p99()
                verdict = judge(rung, self.baseline[mid], self.args)
                self.verdicts[mid].append(verdict)
                if not verdict.ok:
                    self.phase[mid] = "knee"
                    return
                if self.baseline[mid] is None:
                    self.baseline[mid] = pct(rung.latency, 50)
                self.knee[mid] = rung
                index += 1
                if index >= self.args.ramp_max:
                    # Nothing broke. Say that by stopping rather than by
                    # climbing until the arithmetic gets silly.
                    return
                self.rate[mid] = rung.rate * self.args.ramp_factor
        finally:
            if self.phase[mid] != "knee":
                self.phase[mid] = "done"

    async def _pump(self, model: Model, rung: Rung, window: float) -> None:
        """Issue on a schedule and never wait for an answer.

        The schedule is computed from the start of the rung, not from now, so
        a stall does not quietly reschedule everything behind it. That is the
        difference between measuring what a queue does under overload and
        measuring a queue this process kept politely short on the target's
        behalf.
        """
        mid = model.id
        st = self.stats[mid]
        interval = 1.0 / rung.rate if rung.rate > 0 else 0.0
        start = rung.started
        deadline = start + window
        i = 0
        while self.running and clock() < deadline:
            if self.hit_limit():
                return
            if self.rate[mid] != rung.rate:
                return
            now = clock()
            elapsed = now - start
            target = int(elapsed / interval) + 1 if interval else i + 1
            burst = 0
            while i < target and burst < BURST_CAP:
                due = start + i * interval
                i += 1
                burst += 1
                rung.sent += 1
                st.sent += 1
                self.total.sent += 1
                self._sent += 1
                if st.inflight >= self.args.max_inflight:
                    # Counted, never queued. A backlog parked in the harness is
                    # a backlog hidden from the person reading the screen, and
                    # it would come out later as latency the target never caused.
                    rung.dropped += 1
                    st.dropped += 1
                    self.total.dropped += 1
                    continue
                # Booked here rather than inside _one. A coroutine handed to
                # create_task does not run until the next suspension point, so
                # counting there would let a whole burst -- up to BURST_CAP of
                # them -- be admitted against one stale reading of the ceiling.
                self._book(st)
                task = asyncio.create_task(self._one(model, due, rung))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
            if burst:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(max(0.0, (start + i * interval) - clock()))

    async def _settle(self, model: Model) -> None:
        """Let the rung's own backlog drain before judging it.

        Without this a rung inherits the tail of the one before it, and the
        ramp reports a knee one rung early -- every time, in the same
        direction. What is still outstanding when this gives up is the finding.
        """
        st = self.stats[model.id]
        until = clock() + self.args.ramp_settle
        while self.running and st.inflight and clock() < until:
            await asyncio.sleep(0.05)

    # -- one request -------------------------------------------------------

    def _book(self, st: Stats) -> None:
        """Take a slot against the ceiling. Released in _one's finally."""
        st.inflight += 1
        self.total.inflight += 1
        st.peak_inflight = max(st.peak_inflight, st.inflight)
        self.total.peak_inflight = max(self.total.peak_inflight, self.total.inflight)

    def _fail(self, st: Stats, model_id: str, code: int, message: str,
              rung: Rung | None) -> None:
        st.err += 1
        self.total.err += 1
        if rung is not None:
            rung.err += 1
        self.errors[(model_id, code, message)] += 1

    def next_nonce(self) -> str:
        """Unique per request, and unique across runs against the same server.

        A counter alone repeats the moment the harness is restarted, and the
        prefix cache on the other end does not restart with it.
        """
        self._nonce += 1
        return f"{BOOT_NONCE}{self._nonce:x}"

    async def _one(self, model: Model, due: float, rung: Rung | None) -> None:
        url = self.args.base.rstrip("/") + ENDPOINT_FOR_MODALITY[model.modality]
        upload = model.modality == "transcription"
        body: dict = {}
        form: dict = {}
        files: dict = {}
        if upload:
            # No nonce: the clip is fixed for the run. See upload_clip.
            form, files = build_upload(model, self.args)
        else:
            body = build_body(model, self.args, self.next_nonce())
        streaming = bool(body.get("stream"))
        st = self.stats[model.id]

        began = clock()
        delay = max(0.0, began - due)
        st.note_send(began)
        self.total.note_send(began)
        ttft: float | None = None
        cost = 0.0
        counted = 0
        reported: int | None = None
        nbytes = 0
        try:
            if streaming:
                async with self.client.stream("POST", url, json=body) as resp:
                    st.codes[resp.status_code] += 1
                    self.total.codes[resp.status_code] += 1
                    if resp.status_code != 200:
                        raw = await resp.aread()
                        self._fail(st, model.id, resp.status_code,
                                   describe_error(resp.status_code, raw), rung)
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        nbytes += len(payload)
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        for choice in chunk.get("choices") or ():
                            if delta_text(choice.get("delta")):
                                if ttft is None:
                                    ttft = clock() - began
                                counted += 1
                        told = chunk_tokens(chunk)
                        if told is not None:
                            reported = told
                        told_cost = chunk_cost(chunk)
                        if told_cost:
                            cost = told_cost
            else:
                if upload:
                    resp = await self.client.post(url, data=form, files=files)
                else:
                    resp = await self.client.post(url, json=body)
                st.codes[resp.status_code] += 1
                self.total.codes[resp.status_code] += 1
                nbytes = len(resp.content)
                if resp.status_code != 200:
                    self._fail(st, model.id, resp.status_code,
                               describe_error(resp.status_code, resp.content), rung)
                    return
                try:
                    parsed = resp.json()
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    # A transcription answers {"text": ...} and no usage
                    # block, so this leaves `reported` None and the request is
                    # counted at zero tokens. Deliberate: 00-architecture's
                    # audio appendix refuses to invent a token count for an
                    # audio request, and counting the words of the transcript
                    # here would put a number on the screen that the server
                    # never reported. Requests per second is the real figure
                    # for this endpoint; tok/s reads 0 and should.
                    reported = chunk_tokens(parsed)
                    cost = chunk_cost(parsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # connect refused, read timeout, reset
            st.codes[0] += 1
            self.total.codes[0] += 1
            self._fail(st, model.id, 0, f"{type(exc).__name__}: {exc}", rung)
            return
        finally:
            st.inflight -= 1
            self.total.inflight -= 1
            now = clock()
            st.record_end(now)
            self.total.record_end(now)
            if rung is not None:
                rung.send_delay.append(delay)

        done = clock()
        tokens = reported if reported is not None else counted
        for target in (st, self.total):
            target.ok += 1
            target.seq += 1
            target.latency.append(done - due)
            target.service.append(done - began)
            target.send_delay.append(delay)
            target.tokens += tokens
            target.nbytes += nbytes
            target.cost += cost
            if ttft is not None:
                target.ttft.append(ttft)
        if rung is not None:
            rung.ok += 1
            rung.latency.append(done - due)
            rung.service.append(done - began)


#: The delta fields that carry generated text. Three dialects reach this
#: gateway and a reasoning model may fill any of them while leaving `content`
#: an empty string, so counting only `content` reports zero tokens per second
#: for a target that is in fact working hard.
DELTA_TEXT_FIELDS = ("content", "reasoning_content", "reasoning", "refusal")


def delta_text(delta) -> bool:
    if not isinstance(delta, dict):
        return False
    return any(isinstance(delta.get(f), str) and delta[f] for f in DELTA_TEXT_FIELDS)


def chunk_tokens(chunk: dict) -> int | None:
    """What the server says it generated, if it says anything.

    Two dialects reach this gateway: OpenAI's `usage`, and llama.cpp's
    `timings.predicted_n` on the final chunk. Preferred over counting deltas,
    which counts SSE frames rather than tokens whenever a server batches them.
    """
    usage = chunk.get("usage")
    if isinstance(usage, dict):
        for key in ("completion_tokens", "total_tokens"):
            if isinstance(usage.get(key), int) and usage[key]:
                return usage[key]
    timings = chunk.get("timings")
    if isinstance(timings, dict) and isinstance(timings.get("predicted_n"), int):
        return timings["predicted_n"]
    return None


def chunk_cost(chunk: dict) -> float:
    """What the upstream says the request cost, in dollars.

    Only some report it -- OpenRouter puts `usage.cost` on every response --
    and it cannot be reconstructed from a rate card, so it is taken when
    offered and left at zero when not. A load test against paid remote targets
    should not be the thing that tells you afterwards.
    """
    usage = chunk.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("cost"), (int, float)):
        return float(usage["cost"])
    return 0.0


# --------------------------------------------------------------------------
# formatting


def fmt_secs(v: float | None) -> str:
    if v is None:
        return "-"
    if v < 1.0:
        return f"{v * 1000:.0f}ms"
    return f"{v:.2f}s"


def fmt_count(n: int) -> str:
    if n < 10000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"


def sparkline(values, width: int) -> str:
    if not values:
        return ""
    tail = list(values)[-width:]
    top = max(tail)
    if top <= 0:
        return SPARK_CHARS[0] * len(tail)
    return "".join(SPARK_CHARS[min(7, int(v / top * 7.999))] for v in tail)


def row_cells(name: str, st: Stats, now: float, elapsed: float,
              overall: bool = False) -> list[str]:
    # A dash, not a zero, when nothing counted any tokens. An audio request
    # reports none and never will -- 00-architecture's audio appendix refuses
    # to invent one -- and a 0 in this column reads as a target that has
    # stalled rather than one measured in a unit that does not apply. Same
    # rule the deployments strip follows on screen, and the same rule
    # fmt_secs already follows for a TTFT that was never observed.
    tok_s = st.tokens / elapsed if elapsed > 0 else 0.0
    tok_cell = f"{tok_s:.0f}" if st.tokens else "-"
    rate = (st.ok / elapsed if elapsed > 0 else 0.0) if overall else st.rps(now)
    # The summary is drawn once and must not miss the last request measured;
    # the live table is drawn twenty times a second and must not pay for it.
    p50, _p90, p99, ttft = st.quantiles(now, max_age=0.0 if overall else 0.25)
    return [
        name,
        fmt_count(st.sent),
        fmt_count(st.ok),
        fmt_count(st.err),
        fmt_count(st.dropped),
        str(st.inflight),
        f"{rate:.1f}",
        fmt_secs(p50),
        fmt_secs(p99),
        fmt_secs(ttft),
        tok_cell,
    ]


HEADERS = ["MODEL", "REQ", "OK", "ERR", "DROP", "INFL", "RPS", "p50", "p99", "TTFT", "tok/s"]
WIDTHS = [None, 7, 7, 6, 6, 6, 7, 8, 8, 8, 7]


def render_row(cells: list[str], name_width: int) -> str:
    out = [cells[0][:name_width].ljust(name_width)]
    for cell, width in zip(cells[1:], WIDTHS[1:]):
        out.append(cell.rjust(width))
    return " ".join(out)


def ramp_line(runner: Runner, model: Model) -> str:
    """Where this model's ramp has got to, in one line."""
    mid = model.id
    phase = runner.phase[mid]
    rung = runner.current[mid]
    knee = runner.knee[mid]
    if phase == "knee":
        bad = runner.verdicts[mid][-1] if runner.verdicts[mid] else None
        held = (f"held {knee.rate:.1f}/s" if knee else "held nothing")
        who = "harness" if bad and bad.harness else "target"
        return (f"{mid}: knee at {rung.rate:.1f}/s — {held}. "
                f"{who} gave out: {bad.reason if bad else 'unknown'}")
    if rung is None:
        return f"{mid}: idle"
    if not runner.open_loop:
        return ""
    tail = " · settling" if phase == "settling" else ""
    return (f"{mid}: rung {rung.index + 1} at {rung.rate:.1f}/s{tail}"
            + (f" · last held {knee.rate:.1f}/s" if knee else ""))


def refuse(model: Model, args) -> str:
    """Why this model must not be loaded, or "" if it may be.

    One function, because the picker and the command line have to agree: a
    model the TUI greys out and the CLI happily hammers is how a bill happens.
    """
    if not model.drivable:
        return f"{model.id}: {model.why_not}"
    blocked = sorted(set(args.exclude_provider) & set(model.providers))
    if blocked:
        return f"{model.id} is served by {', '.join(blocked)}, which --exclude-provider named."
    if model.metered and not args.include_paid:
        return (f"{model.id} costs money ({model.why_metered}). "
                f"Re-run with --include-paid to spend money on purpose.")
    return ""


# --------------------------------------------------------------------------
# the TUI


class Tui:
    def __init__(self, stdscr, args, models: list[Model]):
        self.stdscr = stdscr
        self.args = args
        self.all_models = models
        self.selected = {m.id for m in models if m.id in args.preselect}
        self.cursor = 0
        self.screen = "run" if self.selected else "pick"
        self.quit = False
        self.runner: Runner | None = None
        self.note = ""
        if self.selected:
            self._build_runner()

    # -- state -------------------------------------------------------------

    def chosen(self) -> list[Model]:
        return [m for m in self.all_models if m.id in self.selected and m.drivable]

    def refuse(self, m: Model) -> str:
        return refuse(m, self.args)

    def _build_runner(self) -> None:
        self.runner = Runner(self.args, self.chosen())

    async def enter_run(self) -> None:
        picked = self.chosen()
        if not picked:
            self.note = "nothing selected that can be driven"
            return
        if self.runner is not None:
            await self.runner.aclose()
        self._build_runner()
        self.screen = "run"
        self.note = ""
        self.runner.start()

    # -- keys --------------------------------------------------------------

    async def key(self, ch: int) -> None:
        if self.screen == "pick":
            await self._key_pick(ch)
        else:
            await self._key_run(ch)

    async def _key_pick(self, ch: int) -> None:
        n = len(self.all_models)
        if ch in (ord("q"), 27):
            self.quit = True
        elif ch in (curses.KEY_DOWN, ord("j")) and n:
            self.cursor = (self.cursor + 1) % n
        elif ch in (curses.KEY_UP, ord("k")) and n:
            self.cursor = (self.cursor - 1) % n
        elif ch == ord(" ") and n:
            model = self.all_models[self.cursor]
            refusal = self.refuse(model)
            if refusal:
                self.note = refusal
            elif model.id in self.selected:
                self.selected.discard(model.id)
            else:
                self.selected.add(model.id)
        elif ch == ord("a"):
            self.selected = {m.id for m in self.all_models if not self.refuse(m)}
            skipped = [m for m in self.all_models if self.refuse(m) and m.drivable]
            self.note = (
                f"{len(skipped)} model{'s' if len(skipped) != 1 else ''} left out; "
                f"--include-paid to load them too" if skipped else ""
            )
        elif ch == ord("n"):
            self.selected.clear()
        elif ch in (curses.KEY_ENTER, 10, 13):
            await self.enter_run()

    async def _key_run(self, ch: int) -> None:
        r = self.runner
        if ch in (ord("q"), 27):
            self.quit = True
        elif ch == ord("s"):
            if r.running:
                r.stop("stopped")
            else:
                r.start()
        elif ch == ord("r"):
            r.reset()
        elif ch == ord("m"):
            r.stop("stopped")
            self.screen = "pick"
        elif ch in (ord("+"), ord("=")):
            self._dial(r, lambda v: v + 1)
        elif ch in (ord("-"), ord("_")):
            self._dial(r, lambda v: v - 1)
        elif ch == ord("]"):
            self._dial(r, lambda v: v + 10)
        elif ch == ord("["):
            self._dial(r, lambda v: v - 10)
        elif ch == ord("}"):
            # Doubling, because ten at a time is not how you find the knee:
            # 8 -> 512 is six keypresses, and the number worth knowing is
            # usually a long way above wherever the run started.
            self._dial(r, lambda v: v * 2)
        elif ch == ord("{"):
            self._dial(r, lambda v: v / 2)

    def _dial(self, r: Runner, fn) -> None:
        """The same keys move whichever number this mode is driven by."""
        if r.open_loop:
            self.note = r.set_rate(fn(r.current_rate()))
        else:
            r.set_concurrency(int(fn(r.concurrency)))

    # -- drawing -----------------------------------------------------------

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        try:
            self.stdscr.addnstr(y, x, text, max(0, w - x - 1), attr)
        except curses.error:
            pass

    def render(self) -> None:
        self.stdscr.erase()
        if self.screen == "pick":
            self._render_pick()
        else:
            self._render_run()
        self.stdscr.noutrefresh()
        curses.doupdate()

    def _render_pick(self) -> None:
        h, w = self.stdscr.getmaxyx()
        self.put(0, 0, f" derate load tester  ·  {self.args.base} ", curses.A_REVERSE)
        self.put(2, 1, "which models should take the load?", curses.A_BOLD)
        y = 4
        for i, m in enumerate(self.all_models):
            if y >= h - 3:
                break
            mark = "x" if m.id in self.selected else " "
            tail = f"{m.modality:<13} {'/'.join(m.providers or m.kinds) or '-':<14}"
            if m.context_length:
                tail += f" ctx {m.context_length}"
            if m.targets:
                tail += f"  {m.targets} target{'s' if m.targets != 1 else ''}"
            if not m.drivable:
                tail += f"  — {m.why_not}"
            elif m.metered:
                tail += "  — $ " + m.why_metered
                if not self.args.include_paid:
                    tail += ", --include-paid"
            attr = curses.A_REVERSE if i == self.cursor else 0
            if self.refuse(m):
                attr |= curses.A_DIM
            self.put(y, 1, f"[{mark}] {m.id}", attr | curses.A_BOLD)
            self.put(y, min(w - 2, 6 + max(28, len(m.id) + 2)), tail, curses.A_DIM)
            y += 1
        if not self.all_models:
            self.put(4, 1, "the gateway is serving no models.", curses.A_DIM)
        if self.note:
            self.put(h - 3, 1, self.note, curses.A_BOLD)
        self.put(h - 1, 0,
                 " [space] toggle  [a] all  [n] none  [enter] run  [q] quit ",
                 curses.A_REVERSE)

    def _render_run(self) -> None:
        r = self.runner
        h, w = self.stdscr.getmaxyx()
        now = clock()
        r.sample(now)
        elapsed = r.elapsed

        state = "RUNNING" if r.running else (r.stop_reason or "IDLE").upper()
        head = f" derate load tester  ·  {self.args.base} "
        self.put(0, 0, head.ljust(max(0, w - 1)), curses.A_REVERSE)
        self.put(0, max(0, w - len(state) - 14), f"{state}  {elapsed:6.1f}s ",
                 curses.A_REVERSE | curses.A_BOLD)

        limit = []
        if self.args.duration:
            limit.append(f"{self.args.duration:g}s")
        if self.args.requests:
            limit.append(f"{self.args.requests} req")
        conf = (
            r.shape_note()
            + f" · {len(r.models)} model{'s' if len(r.models) != 1 else ''} · "
            f"{'stream' if self.args.stream else 'no stream'} · "
            f"max_tokens {self.args.max_tokens}"
        )
        if self.args.hammer:
            conf += f" · ceiling {self.args.max_inflight}"
        # Only when something on screen is actually being uploaded to. See the
        # same line in headless() for why it is worth a slot in the header.
        if any(m.modality == "transcription" for m in r.models):
            conf += " · uploading " + self.args.audio_note
        if limit:
            conf += " · until " + ", ".join(limit)
        if getattr(self.args, "loop_name", ""):
            conf += " · " + self.args.loop_name
        self.put(1, 1, conf, curses.A_DIM)

        spark = sparkline(r.rps_history, max(10, min(120, w - 20)))
        if spark:
            self.put(2, 1, f"rps {r.total.rps(now):7.1f} {spark}")

        name_width = max(16, min(34, max((len(m.id) for m in r.models), default=16)))
        fixed = sum(x for x in WIDTHS[1:]) + len(WIDTHS)
        name_width = max(10, min(name_width, w - fixed - 2))

        y = 4
        header = render_row(HEADERS, name_width)
        self.put(y, 1, header, curses.A_BOLD | curses.A_UNDERLINE)
        y += 1
        for m in r.models:
            if y >= h - 4:
                break
            st = r.stats[m.id]
            attr = curses.A_NORMAL
            if st.err and st.err == st.sent:
                attr = curses.A_BOLD
            self.put(y, 1, render_row(row_cells(m.id, st, now, elapsed), name_width), attr)
            y += 1
        if len(r.models) > 1:
            self.put(y, 1, "─" * min(len(header), max(0, w - 3)), curses.A_DIM)
            y += 1
            self.put(y, 1, render_row(row_cells("TOTAL", r.total, now, elapsed), name_width),
                     curses.A_BOLD)
            y += 1
        y += 1

        if r.open_loop:
            for m in r.models:
                if y >= h - 2:
                    break
                line = ramp_line(r, m)
                if line:
                    self.put(y, 1, line,
                             curses.A_BOLD if r.phase[m.id] == "knee" else curses.A_DIM)
                    y += 1
            y += 1

        for m in r.models:
            if y >= h - 2:
                break
            st = r.stats[m.id]
            held = st.stalled_for(now)
            if held >= self.args.stall_after:
                self.put(y, 1,
                         f"{m.id}: {st.inflight} in flight, nothing back for {held:.0f}s "
                         f"— and only its own",
                         curses.A_BOLD)
                y += 1
        if r.loop_lag >= 0.05 and y < h - 2:
            self.put(y, 1,
                     f"harness loop lag {r.loop_lag * 1000:.0f}ms — this process, "
                     f"not the gateway, is what is capping the rate",
                     curses.A_BOLD)
            y += 1

        codes = ", ".join(
            f"{code or 'net'}×{n}" for code, n in sorted(r.total.codes.items()) if code != 200
        )
        if codes:
            self.put(y, 1, f"non-200: {codes}", curses.A_BOLD)
            y += 1
        if r.errors:
            self.put(y, 1, "errors, as the gateway worded them", curses.A_DIM)
            y += 1
            for (model_id, code, message), n in r.errors.most_common():
                if y >= h - 2:
                    self.put(y, 3, "…more errors than fit; the summary on exit has them all",
                             curses.A_DIM)
                    break
                label = f"{model_id}  {code or 'net'} ×{n}  "
                self.put(y, 3, label, curses.A_BOLD)
                # Wrapped, never cut: a refusal that names what to change is
                # only useful whole.
                for line in wrap(message, max(20, w - 6 - len(label))):
                    if y >= h - 2:
                        break
                    self.put(y, 3 + len(label), line)
                    y += 1

        dial = "rate" if r.open_loop else "conc"
        self.put(h - 1, 0,
                 f" [s] start/stop  [+/-] {dial} ±1  [ [/] ] ±10  [{{/}}] halve/double  "
                 "[r] reset  [m] models  [q] quit ",
                 curses.A_REVERSE)


def wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines, cur = [], ""
    for word in words:
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


async def tui_main(stdscr, args, models: list[Model], finished: list, notes: list) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    tui = Tui(stdscr, args, models)
    pin = None
    if args.pin_policy and tui.chosen():
        pin = PolicyPin(args.base, args.headers, args.timeout, args.pin_policy)
        notes.extend(await pin.apply([m.id for m in tui.chosen()]))
    # Named on the command line means "run it": the picker is for when the
    # selection was not already made.
    if tui.runner is not None:
        tui.runner.start()
    try:
        while not tui.quit:
            ch = stdscr.getch()
            while ch != -1:
                if ch == curses.KEY_RESIZE:
                    stdscr.erase()
                else:
                    await tui.key(ch)
                ch = stdscr.getch()
            if tui.runner is not None:
                tui.runner.tick()
                tui.runner.fill()
            tui.render()
            before = clock()
            await asyncio.sleep(0.05)
            if tui.runner is not None:
                tui.runner.note_loop(0.05, clock() - before)
    finally:
        if tui.runner is not None:
            await tui.runner.aclose()
            # Handed back rather than printed here: this is still inside
            # curses.wrapper, and endwin() would wipe the summary off the
            # screen on the way out.
            finished.append(tui.runner)
        if pin is not None:
            notes.extend(await pin.restore())


# --------------------------------------------------------------------------
# headless


async def headless(args, models: list[Model]) -> int:
    runner = Runner(args, models)
    print(f"→ {args.base}  ·  {runner.shape_note()}"
          f"  ·  {len(models)} model{'s' if len(models) != 1 else ''}  ·  "
          f"{'stream' if args.stream else 'no stream'}"
          + (f"  ·  {args.loop_name}" if args.loop_name else ""))
    # Same reason as in --list: the clip is half of what a transcription
    # figure means, and it is not visible anywhere in the numbers.
    if any(m.modality == "transcription" for m in models):
        print(f"  uploading: {args.audio_note}")

    pin = None
    if args.pin_policy:
        pin = PolicyPin(args.base, args.headers, args.timeout, args.pin_policy)
        for line in await pin.apply([m.id for m in models]):
            print(f"  routing: {line}")

    runner.start()
    last = 0.0

    def progress(tag: str = "") -> None:
        now = clock()
        t = runner.total
        rate = runner.current_rate() if runner.open_loop else 0.0
        offered = f"{rate:6.1f}/s offered  " if runner.open_loop else ""
        print(f"  {runner.elapsed:6.1f}s  {offered}{t.ok:>7} ok  {t.err:>5} err  "
              f"{t.dropped:>5} drop  {t.rps(now):7.1f} rps  "
              f"p50 {fmt_secs(pct(t.latency, 50)):>8}  "
              f"p99 {fmt_secs(pct(t.latency, 99)):>8}  inflight {t.inflight}{tag}")

    seen: dict[str, int] = {m.id: 0 for m in models}

    def announce() -> None:
        """Say what each rung decided, once, as it is decided."""
        for m in models:
            done = len(runner.verdicts[m.id])
            while seen[m.id] < done:
                rung = runner.rungs[m.id][seen[m.id]]
                verdict = runner.verdicts[m.id][seen[m.id]]
                seen[m.id] += 1
                mark = "held" if verdict.ok else "BROKE"
                print(f"    {m.id}  rung {rung.index + 1}  {rung.rate:7.1f}/s offered  "
                      f"{rung.achieved:7.1f}/s back  "
                      f"p50 {fmt_secs(pct(rung.latency, 50))}  "
                      f"p99 {fmt_secs(pct(rung.latency, 99))}  {mark}")
                if not verdict.ok:
                    for line in wrap(verdict.reason, 88):
                        print(f"      {line}")

    try:
        while runner.running:
            before = clock()
            await asyncio.sleep(0.2)
            runner.note_loop(0.2, clock() - before)
            runner.tick()
            announce()
            if clock() - last >= 1.0:
                last = clock()
                progress()
        announce()
        # What was already issued is given a chance to finish. It was sent
        # inside the window, and scoring it as an error would blame the target
        # for the clock running out.
        drain_until = clock() + args.drain
        while runner.total.inflight and clock() < drain_until:
            await asyncio.sleep(0.2)
            if clock() - last >= 1.0:
                last = clock()
                progress("  draining")
    except (KeyboardInterrupt, asyncio.CancelledError):
        runner.stop("interrupted")
    finally:
        await runner.aclose()
        if pin is not None:
            for line in await pin.restore():
                print(f"  routing: {line}")
    print_summary(runner, args)
    return 1 if runner.total.err else 0


def print_summary(runner: Runner, args) -> None:
    if not runner.total.sent:
        return
    now = clock()
    elapsed = runner.elapsed
    name_width = max(16, max((len(m.id) for m in runner.models), default=16))
    print()
    print(f"  {elapsed:.1f}s, {runner.stop_reason or 'stopped'}")
    print("  " + render_row(HEADERS, name_width))
    for m in runner.models:
        print("  " + render_row(
            row_cells(m.id, runner.stats[m.id], now, elapsed, overall=True), name_width))
    if len(runner.models) > 1:
        print("  " + render_row(
            row_cells("TOTAL", runner.total, now, elapsed, overall=True), name_width))

    # Only a ramp has a knee. A run held at one rate never asked the question,
    # and "no rung held" reads like a failure rather than like a run that was
    # never climbing in the first place.
    climbed = any(runner.verdicts[m.id] or runner.knee[m.id] for m in runner.models)
    if runner.open_loop and climbed:
        print("\n  how hard it went before it stopped holding:")
        for m in runner.models:
            knee = runner.knee[m.id]
            bad = next((v for v in runner.verdicts[m.id] if not v.ok), None)
            st = runner.stats[m.id]
            if knee is None:
                print(f"    {m.id}: no rung held" + (f" — {bad.reason}" if bad else ""))
            else:
                print(f"    {m.id}: held {knee.rate:.1f}/s offered, "
                      f"{knee.achieved:.1f}/s back, p50 {fmt_secs(pct(knee.latency, 50))}, "
                      f"p99 {fmt_secs(pct(knee.latency, 99))}, "
                      f"peak {st.peak_inflight} in flight")
            if bad is not None:
                who = "the harness, not the target" if bad.harness else "the target"
                for line in wrap(f"broke at the next rung — {who}: {bad.reason}", 86):
                    print(f"      {line}")
        delay = pct(runner.total.send_delay, 99)
        if delay and delay > 0.05:
            print(f"    send delay p99 {fmt_secs(delay)} — requests went out that far "
                  f"behind their scheduled moment, so latency above includes a queue "
                  f"this process was keeping")

    codes = ", ".join(f"{code or 'net'}×{n}" for code, n in sorted(runner.total.codes.items()))
    print(f"\n  status: {codes}")
    if runner.total.dropped:
        print(f"  {runner.total.dropped} arrivals never sent: {args.max_inflight} were "
              f"already outstanding. That is backlog, not a harness limit to raise.")
    if runner.total.abandoned:
        print(f"  {runner.total.abandoned} in flight when the run stopped, "
              f"dropped unmeasured — RPS and percentiles do not include them")
        stuck = [(m.id, runner.stats[m.id].abandoned) for m in runner.models
                 if runner.stats[m.id].abandoned]
        if len(stuck) > 1 or (stuck and len(runner.models) > 1):
            print("    " + ", ".join(f"{name} ×{n}" for name, n in stuck))
    if runner.total.cost:
        print(f"  ${runner.total.cost:.4f} reported by the upstreams that price themselves")
    if runner.loop_lag >= 0.05:
        print(f"  harness loop lag peaked at {runner.loop_lag * 1000:.0f}ms — this process, "
              f"not the gateway, was capping the rate")
    if runner.errors:
        print("\n  errors, as the gateway worded them:")
        for (model_id, code, message), n in runner.errors.most_common():
            print(f"    {model_id}  {code or 'net'} ×{n}")
            for line in wrap(message, 92):
                print(f"      {line}")


# --------------------------------------------------------------------------


def install_fast_loop() -> str:
    """Swap in uvloop when it is installed, and say so.

    At a few hundred streams in flight the selector loop starts to be the
    thing under test rather than the gateway. uvloop is not a dependency of
    anything here, so its absence is a slower run and never an error.
    """
    try:
        import uvloop
    except ImportError:
        return ""
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    return f"uvloop {getattr(uvloop, '__version__', '')}".strip()


def raise_fd_limit() -> int:
    """Take the highest number of open files the OS will hand over.

    Every request in flight is a socket, so a soft limit of 1024 turns a
    `-c 2000` run into a wall of `OSError: [Errno 24]` that reads exactly like
    the gateway refusing connections. Raising the soft limit to the hard one
    needs no privileges; the hard limit is left where it is. Returns the soft
    limit in force, or 0 where there is no such thing to ask.
    """
    try:
        import resource
    except ImportError:  # not a POSIX box
        return 0
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < hard:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            soft = hard
        except (ValueError, OSError):
            pass
    return soft


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="loadtest.py",
        description="Load-test the derate gateway: one model or all of them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--base", default=DEFAULT_BASE,
                   help=f"gateway base URL (default {DEFAULT_BASE}, $DERATE_BASE_URL)")
    p.add_argument("--model", "-m", action="append", default=[],
                   help="model to hit; repeatable. Omit to pick in the TUI.")
    p.add_argument("--all", action="store_true",
                   help="hit every model that can be driven for free")
    p.add_argument("--include-paid", "--include-remote", dest="include_paid",
                   action="store_true",
                   help="also load models that cost money. Off by default: a "
                        "sustained load test against a metered API is a bill.")
    p.add_argument("--exclude-provider", action="append", default=[], metavar="ID",
                   help="never load anything this provider serves; repeatable "
                        "(e.g. --exclude-provider openrouter)")

    p.add_argument("--hammer", action="store_true",
                   help="open loop, ramping, heavy cache-busting requests, and the "
                        "router taken off any rotation: push until something breaks "
                        "and report where")
    p.add_argument("--concurrency", "-c", type=int, default=8,
                   help="closed loop: requests in flight per model. Each model has "
                        "its own slots, so a target that stops answering can only "
                        "hold its own (default 8)")
    p.add_argument("--max-inflight", type=int, default=20000,
                   help="open loop: outstanding requests per model before arrivals "
                        "are dropped rather than queued (default 20000)")
    p.add_argument("--rps", type=float, default=0.0,
                   help="open loop at this arrival rate per model, no ramp")

    p.add_argument("--no-ramp", dest="ramp", action="store_false",
                   help="--hammer at one fixed rate instead of climbing")
    p.add_argument("--ramp-start", type=float, default=8.0,
                   help="first rung, arrivals per second per model (default 8)")
    p.add_argument("--ramp-factor", type=float, default=2.0,
                   help="rung multiplier (default 2, i.e. doubling)")
    p.add_argument("--ramp-step", type=float, default=10.0,
                   help="seconds to hold each rung (default 10)")
    p.add_argument("--ramp-settle", type=float, default=2.0,
                   help="seconds to let a rung's backlog drain before judging it "
                        "(default 2). What is still in flight then fails the rung.")
    p.add_argument("--ramp-err", type=float, default=ERROR_RATE_MAX,
                   help=f"error rate that fails a rung (default {ERROR_RATE_MAX})")
    p.add_argument("--ramp-max", type=int, default=16,
                   help="stop after this many rungs even if nothing broke (default 16)")

    p.add_argument("--max-tokens", type=int, default=None,
                   help="completion length (default 64, or 2048 under --hammer)")
    p.add_argument("--prompt-tokens", type=int, default=0,
                   help="pad the prompt to roughly this many tokens. 0 sends --prompt "
                        "as written, except under --hammer, which fills half the "
                        "context window.")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--prompt", default="Name three things a GPU cluster is good at.")
    p.add_argument("--voice", default="alloy", help="for speech models")
    p.add_argument("--audio", default="", metavar="PATH",
                   help="for transcription models: upload this clip instead of a "
                        "synthesised one. Read once at startup and sent as is.")
    p.add_argument("--audio-seconds", type=float, default=AUDIO_SECONDS,
                   help=f"length of the synthesised clip when --audio is not given "
                        f"(default {AUDIO_SECONDS:g}s, one Whisper window)")
    p.add_argument("--language", default="",
                   help="for transcription models: the language form field, sent "
                        "only when given (e.g. --language en). An English-only "
                        "checkpoint needs it -- vLLM runs language detection "
                        "when the field is absent, and whisper-*.en has no "
                        "language tokens to detect with, so every request 500s.")
    p.add_argument("--no-stream", dest="stream", action="store_false",
                   help="one response instead of SSE; TTFT is unavailable without streaming")

    p.add_argument("--pin-policy", default=None, metavar="POLICY",
                   help="pin the router to this policy for the run and put it back "
                        "afterwards. Defaults to least_outstanding under --hammer, so "
                        "the router stops taking turns while you are measuring it.")
    p.add_argument("--no-pin-policy", dest="pin_policy", action="store_const", const="",
                   help="leave the routing policy exactly as it is")

    p.add_argument("--duration", "-d", type=float, default=0.0,
                   help="stop after this many seconds")
    p.add_argument("--requests", "-n", type=int, default=0,
                   help="stop after this many requests")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--drain", type=float, default=15.0,
                   help="seconds to let in-flight requests finish after the run stops")
    p.add_argument("--api-key", default=os.environ.get("DERATE_API_KEY", ""),
                   help="sent as a bearer token if the gateway wants one")
    p.add_argument("--stall-after", type=float, default=5.0,
                   help="call a model stalled once it has held requests in flight this "
                        "long with nothing coming back (default 5s)")
    p.add_argument("--no-uvloop", action="store_true",
                   help="use the stdlib event loop even where uvloop is installed")
    p.add_argument("--no-tui", action="store_true", help="run headless and print a summary")
    p.add_argument("--list", action="store_true", help="print what is being served, then exit")
    args = p.parse_args(argv)

    # Defaults that depend on the mode, resolved here so nothing downstream has
    # to ask "was this the default or did somebody mean it".
    if args.max_tokens is None:
        args.max_tokens = 2048 if args.hammer else 64
    if args.pin_policy is None:
        args.pin_policy = "least_outstanding" if args.hammer else ""

    # No content-type here, and that is load-bearing rather than an
    # oversight. A header set on the client wins over the one httpx computes
    # per request, so pinning application/json would overwrite the multipart
    # boundary on a transcription upload -- and the gateway refuses a
    # transcription whose body is not multipart, so every upload would come
    # back 400 invalid_content_type and read as a bug on the server. httpx
    # sets application/json for json= by itself; it needs no help.
    args.headers = {}
    if args.api_key:
        args.headers["authorization"] = f"Bearer {args.api_key}"

    # The clip, read once. A run sends it thousands of times and re-reading it
    # per request would measure this process's disk.
    args.audio_bytes = None
    args.audio_name = "clip.wav"
    args.audio_type = "audio/wav"
    args.audio_note = "%.6gs synthesised" % args.audio_seconds
    if args.audio:
        try:
            args.audio_bytes = open(args.audio, "rb").read()
        except OSError as exc:
            p.error(f"--audio {args.audio}: {exc.strerror or exc}")
        if len(args.audio_bytes) > MAX_AUDIO_UPLOAD_BYTES:
            p.error(
                "--audio %s is %.1f MiB, over the gateway's %d MiB upload limit "
                "(max_audio_upload_bytes). Every request would be refused 413."
                % (args.audio, len(args.audio_bytes) / 1048576,
                   MAX_AUDIO_UPLOAD_BYTES // 1048576)
            )
        args.audio_name = os.path.basename(args.audio) or "clip"
        args.audio_type = AUDIO_TYPES.get(
            os.path.splitext(args.audio_name)[1].lower(), "application/octet-stream"
        )
        args.audio_note = "%s, %.1f MiB" % (
            args.audio_name, len(args.audio_bytes) / 1048576
        )
    return args


def main(argv: list[str] | None = None) -> int:
    locale.setlocale(locale.LC_ALL, "")
    args = parse_args(argv if argv is not None else sys.argv[1:])
    args.loop_name = "" if args.no_uvloop else install_fast_loop()
    args.fd_limit = raise_fd_limit()
    open_loop = bool(args.hammer or args.rps)
    ceiling = args.max_inflight if open_loop else args.concurrency
    if args.fd_limit and ceiling > args.fd_limit - 64:
        print(f"{ceiling} requests in flight per model against a limit of "
              f"{args.fd_limit} open files: raise it with `ulimit -n` or the run "
              f"will fail as connection errors that look like the gateway's.",
              file=sys.stderr)

    try:
        models, notes = asyncio.run(discover(args.base, args.headers, args.timeout))
    except httpx.HTTPStatusError as exc:
        print(f"{args.base}/v1/models answered {exc.response.status_code}: "
              f"{describe_error(exc.response.status_code, exc.response.content)}",
              file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"cannot reach {args.base}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(note, file=sys.stderr)

    if args.list:
        for m in models:
            where = "/".join(m.providers or m.kinds) or "-"
            note = ""
            if not m.drivable:
                note = f"   ({m.why_not})"
            elif m.metered:
                note = f"   ($ {m.why_metered})"
            print(f"{m.id:<40} {m.modality:<14} {where:<22} "
                  f"ctx {m.context_length or '-'}{note}")
        # What a transcription run would actually upload. Said here because a
        # requests-per-second figure off this endpoint is meaningless without
        # it: the same server answers a 30s clip and a 4s one at different
        # rates, and nothing in the numbers says which was sent.
        if any(m.modality == "transcription" for m in models):
            print(f"\nuploading to /v1/audio/transcriptions: {args.audio_note}")
        return 0

    if not models:
        print(f"{args.base} is serving no models.", file=sys.stderr)
        return 2

    known = {m.id for m in models}
    unknown = [name for name in args.model if name not in known]
    if unknown:
        print("no such model: " + ", ".join(unknown), file=sys.stderr)
        print("being served: " + ", ".join(sorted(known)), file=sys.stderr)
        return 2

    if args.all:
        args.preselect = {m.id for m in models if not refuse(m, args)}
        left_out = [m for m in models if m.drivable and refuse(m, args)]
        for m in left_out:
            print("not loading " + refuse(m, args), file=sys.stderr)
    else:
        args.preselect = set(args.model)
        # Named explicitly, so this is not a mistake about which model -- but
        # sending sustained load somewhere metered should still be a thing you
        # typed rather than a thing that happened.
        refusals = [refuse(m, args) for m in models
                    if m.id in args.preselect and refuse(m, args)]
        if refusals:
            for line in refusals:
                print(line, file=sys.stderr)
            return 2

    headless_mode = args.no_tui or not sys.stdout.isatty()
    if headless_mode:
        if not args.preselect:
            print("headless needs --model or --all, and everything --all found was "
                  "left out. --include-paid, or --exclude-provider fewer things.",
                  file=sys.stderr)
            return 2
        if not args.duration and not args.requests and not args.hammer:
            # A ramp decides for itself when it is finished; a flat run does not.
            args.duration = 10.0
        chosen = [m for m in models if m.id in args.preselect and m.drivable]
        if not chosen:
            print("nothing selected that can be driven.", file=sys.stderr)
            return 2
        return asyncio.run(headless(args, chosen))

    finished: list[Runner] = []
    notes = []
    try:
        curses.wrapper(
            lambda scr: asyncio.run(tui_main(scr, args, models, finished, notes))
        )
    except KeyboardInterrupt:
        pass
    for line in notes:
        print(f"routing: {line}")
    for runner in finished:
        print_summary(runner, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
