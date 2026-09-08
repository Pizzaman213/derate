"""The load tester's own gates.

`loadtest.py` is a client of the published API and imports nothing from
control_plane, so nothing in the suite would otherwise touch it. What is
pinned here is the handful of things that are wrong silently: a paced run that
sends one request and stops, a prompt a prefix cache can answer, a rung that
blames the target for the harness's own stall, and the classification that
decides whether a run costs money.
"""

from __future__ import annotations

import asyncio
import io
import wave

import httpx
import pytest

import loadtest
from loadtest import (
    HAMMER_PROMPT_FALLBACK,
    MAX_AUDIO_UPLOAD_BYTES,
    Model,
    Rung,
    Runner,
    build_upload,
    classify_providers,
    judge,
    max_tokens_for,
    parse_args,
    prompt_tokens_for,
    refuse,
    synth_prompt,
    synth_wav,
)

#: The two providers this box actually has, trimmed to the fields that decide.
OLLAMA_ROW = {
    "provider_id": "connor-pi-ollama",
    "kind": "ollama",
    "api_key_ref": "",
    "key_state": "not_needed",
    "daily_budget_usd": None,
    "models": [
        {
            "served_name": "qwen2.5:0.5b",
            "input_cost_per_mtok": None,
            "output_cost_per_mtok": None,
        }
    ],
}
OPENROUTER_ROW = {
    "provider_id": "openrouter",
    "kind": "openrouter",
    "api_key_ref": "DERATE_OPENROUTER_API_KEY",
    "key_state": "present",
    "daily_budget_usd": None,
    "models": [
        {
            "served_name": "inclusionai/ling-3.0-flash",
            "input_cost_per_mtok": 0.1,
            "output_cost_per_mtok": 0.4,
        }
    ],
}


def make_model(name="m", **kw):
    base = dict(
        id=name,
        modality="text",
        context_length=4096,
        kinds=("local",),
        targets=1,
    )
    base.update(kw)
    return Model(**base)


# -- what costs money ------------------------------------------------------


def test_a_free_remote_provider_is_not_a_paid_one():
    """The Pi on the LAN is remote and free; OpenRouter is remote and not.

    Splitting on `remote` refused both, which on a cluster whose only local
    runtime is elsewhere means refusing to load anything at all.
    """
    serving, charging = classify_providers([OLLAMA_ROW, OPENROUTER_ROW])
    assert serving["qwen2.5:0.5b"] == {"connor-pi-ollama"}
    assert "qwen2.5:0.5b" not in charging
    assert charging["inclusionai/ling-3.0-flash"] == {"openrouter"}

    free = make_model("qwen2.5:0.5b", kinds=("remote",), providers=("connor-pi-ollama",))
    paid = make_model(
        "inclusionai/ling-3.0-flash",
        kinds=("remote",),
        providers=("openrouter",),
        charged_by=("openrouter",),
    )
    assert not free.metered
    assert paid.metered
    assert "openrouter" in paid.why_metered


def test_a_provider_with_a_budget_or_a_key_counts_as_paid_even_unpriced():
    budgeted = dict(OLLAMA_ROW, provider_id="metered", daily_budget_usd=5.0)
    _, charging = classify_providers([budgeted])
    assert charging["qwen2.5:0.5b"] == {"metered"}


def test_unreadable_pricing_falls_back_to_refusing_every_remote():
    """Never assume free. Refusing a free target is an inconvenience;
    hammering a metered one because the pricing read failed is a bill."""
    unknown = make_model("x", kinds=("remote",), priced_known=False)
    assert unknown.metered
    assert "could not be read" in unknown.why_metered
    local = make_model("y", kinds=("local",), priced_known=False)
    assert not local.metered


def test_exclude_provider_and_include_paid_agree_with_each_other():
    paid = make_model("m", providers=("openrouter",), charged_by=("openrouter",))
    assert "costs money" in refuse(paid, parse_args([]))
    assert refuse(paid, parse_args(["--include-paid"])) == ""
    # The old spelling still works; it is in the docstring's examples.
    assert refuse(paid, parse_args(["--include-remote"])) == ""
    named = parse_args(["--include-paid", "--exclude-provider", "openrouter"])
    assert "--exclude-provider" in refuse(paid, named)


def test_a_model_that_cannot_be_driven_is_refused_before_anything_about_money():
    """A modality this harness has never heard of. Not a skip to be fixed by
    adding it to DRIVABLE -- nothing here knows what to send one."""
    alien = make_model("something", modality="video")
    assert "unknown modality" in refuse(alien, parse_args(["--include-paid"]))


def test_a_transcription_model_is_driven_rather_than_listed_and_skipped():
    """It used to be refused for having no audio file. Whisper pads every clip
    to the same thirty-second window, so a synthesised one is the same load as
    a recorded one and the endpoint is measurable without a corpus."""
    whisper = make_model("whisper", modality="transcription")
    assert whisper.drivable
    assert whisper.why_not == ""
    assert refuse(whisper, parse_args([])) == ''


# -- what gets sent --------------------------------------------------------


def test_the_prompt_nonce_comes_first_so_a_prefix_cache_cannot_answer():
    """Prefix caches hash blocks in order. A nonce anywhere but the front
    leaves every block before it reusable, and the run measures the cache."""
    a = synth_prompt(200, "aaaa", "tail")
    b = synth_prompt(200, "bbbb", "tail")
    assert a.startswith("#aaaa") and b.startswith("#bbbb")
    assert a != b
    # Roughly the requested length, and it says so: CHARS_PER_TOKEN is a rule
    # of thumb, not a tokenizer.
    assert 200 * 2 < len(a) < 200 * 6


def test_a_zero_length_request_still_carries_its_nonce():
    assert synth_prompt(0, "beef", "hello").startswith("#beef ")


def test_a_heavy_prompt_leaves_room_for_the_completion():
    """Prompt plus completion has to fit, or the run measures the validator."""
    args = parse_args(["--hammer"])
    model = make_model(context_length=4096)
    tokens = prompt_tokens_for(model, args)
    assert tokens > 0
    assert tokens + max_tokens_for(model, args, tokens) <= 4096


def test_an_unknown_context_length_does_not_produce_an_empty_prompt():
    """An Ollama provider reports context_length 0 for everything it serves,
    and half of zero is a prompt that measures nothing."""
    args = parse_args(["--hammer"])
    model = make_model(context_length=0)
    assert prompt_tokens_for(model, args) == HAMMER_PROMPT_FALLBACK
    assert max_tokens_for(model, args, HAMMER_PROMPT_FALLBACK) == args.max_tokens


def test_without_hammer_the_prompt_is_the_one_that_was_typed():
    args = parse_args(["--prompt", "hello"])
    assert prompt_tokens_for(make_model(), args) == 0
    body = loadtest.build_body(make_model(), args, "n")
    assert body["messages"][0]["content"].endswith("hello")


def test_speech_is_not_made_heavier_by_a_longer_script():
    """A TTS server synthesises in real time, so length buys a slow request
    rather than a hard one. Pressure there comes from arrival rate."""
    args = parse_args(["--hammer"])
    body = loadtest.build_body(make_model(modality="speech", context_length=0), args, "n")
    assert len(body["input"]) <= loadtest.SPEECH_PROMPT_MAX_CHARS


# -- what gets uploaded ----------------------------------------------------


def test_the_synthesised_clip_is_a_wav_a_decoder_will_open():
    """Hand-built from `struct`, so the harness needs no encoder installed --
    but it has to be a real RIFF file or the server refuses it and the run
    measures the validator. `wave` is the stdlib's opinion on that."""
    clip = synth_wav(0.25, 16000, "cafe0001")
    with wave.open(io.BytesIO(clip)) as f:
        assert f.getnchannels() == 1
        assert f.getsampwidth() == 2
        assert f.getframerate() == 16000
        assert f.getnframes() == 4000  # 0.25s at 16 kHz, to the frame
    # And not silence: a decoder opening a file of zeroes proves less.
    assert set(clip[44:]) != {0}


def test_two_runs_do_not_upload_the_same_bytes():
    """The nonce is per run, not per request. A server that kept yesterday's
    answer must not be able to hand it back; within one run the clip is fixed
    on purpose, because --audio cannot vary either."""
    assert synth_wav(0.1, 16000, "aaaaaaaa") != synth_wav(0.1, 16000, "bbbbbbbb")
    assert synth_wav(0.1, 16000, "aaaaaaaa") == synth_wav(0.1, 16000, "aaaaaaaa")


def test_the_clip_is_built_once_and_not_per_request():
    """Thirty seconds of 16 kHz mono is 480k samples. Generating it per
    request would spend the event loop on filler while requests wait to be
    issued, and land in the numbers as send delay -- as the target's fault."""
    args = parse_args([])
    model = make_model("whisper", modality="transcription")
    _, first = build_upload(model, args)
    _, again = build_upload(model, args)
    assert first["file"][1] is again["file"][1]


def test_the_upload_names_the_model_in_the_form_beside_the_clip():
    """The model is a form field here, not a JSON key: the gateway reads it
    back out of the raw multipart body and never re-encodes the form."""
    args = parse_args(["--audio-seconds", "0.1", "--language", "en"])
    data, files = build_upload(make_model("whisper-base.en", modality="transcription"), args)
    assert data["model"] == "whisper-base.en"
    assert data["language"] == "en"
    name, clip, content_type = files["file"]
    assert name == "clip.wav" and content_type == "audio/wav"
    assert clip.startswith(b"RIFF")


def test_the_language_field_is_absent_rather_than_empty_when_not_asked_for():
    """An empty language is not the same request as no language: OpenAI's
    endpoint treats the field as a hint and an empty one as a bad value."""
    data, _ = build_upload(make_model("w", modality="transcription"), parse_args([]))
    assert "language" not in data


def test_a_client_content_type_would_overwrite_the_multipart_boundary():
    """The headers this harness sets on its client must not include one.

    httpx computes `multipart/form-data; boundary=...` per request, and a
    header pinned on the client wins over it -- so a client-level
    application/json would send an upload the gateway refuses 400
    invalid_content_type, which reads as a bug on the server.
    """
    args = parse_args([])
    assert "content-type" not in {k.lower() for k in args.headers}

    client = httpx.Client(headers=args.headers)
    data, files = build_upload(make_model("w", modality="transcription"),
                               parse_args(["--audio-seconds", "0.1"]))
    request = client.build_request("POST", "http://x/v1/audio/transcriptions",
                                   data=data, files=files)
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")


def test_an_audio_file_over_the_gateway_limit_is_refused_by_name(tmp_path):
    """25 MiB is the gateway's max_audio_upload_bytes. Caught here, where the
    message can name the file, rather than as a 413 per request once a run is
    already going."""
    big = tmp_path / "big.wav"
    big.write_bytes(b"\x00" * (MAX_AUDIO_UPLOAD_BYTES + 1))
    with pytest.raises(SystemExit):
        parse_args(["--audio", str(big)])


def test_an_unreadable_audio_file_stops_the_run_rather_than_starting_one(tmp_path):
    with pytest.raises(SystemExit):
        parse_args(["--audio", str(tmp_path / "nope.wav")])


def test_a_real_clip_is_sent_as_is_under_the_type_its_name_implies(tmp_path):
    """Read once at startup and forwarded byte for byte: the gateway does not
    re-encode the form, so what is opened here is what the runtime decodes."""
    clip = tmp_path / "spoken.flac"
    clip.write_bytes(b"fLaC" + b"\x01\x02\x03")
    args = parse_args(["--audio", str(clip)])
    _, files = build_upload(make_model("w", modality="transcription"), args)
    name, sent, content_type = files["file"]
    assert name == "spoken.flac"
    assert content_type == "audio/flac"
    assert sent == b"fLaC" + b"\x01\x02\x03"
    assert "spoken.flac" in args.audio_note


def test_a_column_with_no_tokens_in_it_shows_a_dash_not_a_zero():
    """An audio request reports no token count and never will. A 0 there reads
    as a target that has stalled; the dash says the unit does not apply."""
    st = loadtest.Stats()
    st.ok = st.sent = 40
    assert loadtest.row_cells("whisper", st, now=1.0, elapsed=1.0)[-1] == "-"
    st.tokens = 400
    assert loadtest.row_cells("qwen", st, now=1.0, elapsed=1.0)[-1] == "400"


# -- judging a rung --------------------------------------------------------


def rung_with(**kw) -> Rung:
    base = dict(index=0, rate=10.0, started=0.0, ended=1.0, sent=10, ok=10)
    base.update(kw)
    rung = Rung(**{k: v for k, v in base.items() if k in Rung.__dataclass_fields__})
    rung.latency = kw.get("latency", [0.01] * rung.ok)
    return rung


def test_a_rung_blames_the_harness_before_it_blames_the_target():
    """Every symptom of a saturated server is also a symptom of a starved
    generator, so the order these are checked in is the finding."""
    args = parse_args([])
    starved = rung_with(ok=0, err=10)
    starved.lag_p99 = 0.5
    verdict = judge(starved, None, args)
    assert not verdict.ok and verdict.harness

    same = rung_with(ok=0, err=10)
    same.lag_p99 = 0.0
    verdict = judge(same, None, args)
    assert not verdict.ok and not verdict.harness
    assert "errors" in verdict.reason


def test_dropped_arrivals_fail_a_rung_and_are_called_backlog():
    args = parse_args([])
    verdict = judge(rung_with(dropped=3), None, args)
    assert not verdict.ok
    assert "not keeping up" in verdict.reason


def test_a_backlog_that_outlives_the_rung_fails_it():
    args = parse_args([])
    verdict = judge(rung_with(left_inflight=7), None, args)
    assert not verdict.ok and "outlived" in verdict.reason


def test_a_tail_blowup_is_measured_against_the_first_rung_that_held():
    args = parse_args([])
    slow = rung_with(latency=[2.0] * 10)
    assert judge(slow, 0.01, args).ok is False
    assert judge(slow, None, args).ok is True  # no baseline yet, nothing to compare


def test_a_rung_that_never_answered_is_a_failure_not_a_pass():
    args = parse_args([])
    verdict = judge(rung_with(ok=0, err=0, sent=10), None, args)
    assert not verdict.ok and "nothing came back" in verdict.reason


# -- the loop --------------------------------------------------------------


def stub_client(handler=None) -> httpx.AsyncClient:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"completion_tokens": 2},
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler or ok))


def test_a_paced_run_keeps_sending_instead_of_stopping_after_one_request():
    """The regression this replaced: --rps overwrote each worker's slot number
    with a timestamp, so `models_for(slot)` raised TypeError inside a task
    nobody awaited, `_live.discard(float)` left the int slot marked live, and
    the pool was never restaffed. Every worker sent exactly one request and
    the run went silent with no error anywhere on the screen.
    """
    async def go():
        args = parse_args(["--rps", "60", "--duration", "1", "--no-tui", "--no-stream"])
        runner = Runner(args, [make_model()])
        runner.client = stub_client()
        runner.start()
        while runner.running:
            await asyncio.sleep(0.02)
            runner.tick()
        sent = runner.stats["m"].ok
        await runner.aclose()
        return sent

    assert asyncio.run(go()) > 20


def test_every_model_is_driven_by_its_own_issuer_with_nothing_shared():
    """No rotation: one model going silent must not cost another one anything.

    The old driver handed each worker a stride of the model list, so with
    fewer slots than models a stalled target took the slot its neighbours were
    waiting on.
    """
    async def go():
        args = parse_args(["--rps", "40", "--duration", "1", "--no-tui", "--no-stream"])
        fast, slow = make_model("fast"), make_model("slow")

        async def handler(request: httpx.Request) -> httpx.Response:
            if b'"slow"' in request.content:
                await asyncio.sleep(30)
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

        runner = Runner(args, [fast, slow])
        runner.client = stub_client(handler)
        runner.start()
        while runner.running:
            await asyncio.sleep(0.02)
            runner.tick()
        counts = (runner.stats["fast"].ok, runner.stats["slow"].ok)
        await runner.aclose()
        return counts

    fast_ok, slow_ok = asyncio.run(go())
    assert slow_ok == 0
    assert fast_ok > 20


def test_arrivals_over_the_ceiling_are_dropped_and_never_queued():
    """A backlog parked in the harness is a backlog hidden from the screen,
    and it comes back later as latency the target did not cause."""
    async def go():
        args = parse_args(["--rps", "500", "--max-inflight", "4", "--no-tui"])
        model = make_model()
        runner = Runner(args, [model])

        async def never(model, due, rung):
            # The runner books the slot before creating the task; releasing it
            # is _one's job, and this stands in for _one.
            try:
                await asyncio.sleep(30)
            finally:
                runner.stats[model.id].inflight -= 1

        runner._one = never
        runner.running = True
        runner._since = loadtest.clock()
        rung = Rung(index=0, rate=500.0, started=loadtest.clock())
        await runner._pump(model, rung, 0.3)
        for task in list(runner._inflight):
            task.cancel()
        return rung

    rung = asyncio.run(go())
    assert rung.sent > rung.dropped > 0
    # Exactly the ceiling got through, and not one more. The count is taken
    # before the task is created, so a burst cannot be admitted against a
    # reading of inflight that none of it has updated yet.
    assert rung.dropped == rung.sent - 4


def test_latency_is_measured_from_when_the_request_was_due():
    """Coordinated omission: starting the clock at send time reports a flat
    latency while the queue behind it explodes."""
    async def go():
        args = parse_args(["--rps", "10", "--no-tui", "--no-stream"])
        model = make_model()
        runner = Runner(args, [model])
        runner.client = stub_client()
        runner.running = True
        runner._since = loadtest.clock()
        rung = Rung(index=0, rate=10.0, started=loadtest.clock())
        # Due a quarter of a second ago: the wait counts, the round trip barely does.
        await runner._one(model, loadtest.clock() - 0.25, rung)
        await runner.aclose()
        return runner.stats["m"]

    st = asyncio.run(go())
    assert st.latency[0] >= 0.25
    assert st.service[0] < 0.25
    assert st.send_delay[0] >= 0.25


def test_the_ramp_stops_climbing_at_the_first_rung_that_breaks():
    async def go():
        args = parse_args([
            "--hammer", "--no-tui", "--ramp-start", "20", "--ramp-step", "0.4",
            "--ramp-settle", "0.2", "--ramp-max", "4", "--no-stream",
        ])
        model = make_model()
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            # Fine at first, then nothing but refusals: a knee with a clear cause.
            if calls["n"] > 12:
                return httpx.Response(503, json={"error": {"message": "no capacity"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

        runner = Runner(args, [model])
        runner.client = stub_client(handler)
        runner.start()
        while runner.running:
            await asyncio.sleep(0.02)
            runner.tick()
        state = (runner.phase["m"], runner.knee["m"], runner.verdicts["m"])
        await runner.aclose()
        return state

    phase, knee, verdicts = asyncio.run(go())
    assert phase == "knee"
    assert any(not v.ok for v in verdicts)
    assert knee is None or knee.rate < 20 * 2 ** 4


@pytest.mark.parametrize("argv,open_loop", [
    ([], False),
    (["--hammer"], True),
    (["--rps", "5"], True),
])
def test_the_mode_follows_from_the_flags(argv, open_loop):
    runner = Runner(parse_args(argv + ["--no-tui"]), [make_model()])
    assert runner.open_loop is open_loop
    # A hand-set rate is a rate, not a starting point for a climb.
    assert runner.ramping is (open_loop and "--hammer" in argv)


def test_hammer_pins_the_router_off_its_rotation_by_default():
    assert parse_args(["--hammer"]).pin_policy == "least_outstanding"
    assert parse_args(["--hammer", "--no-pin-policy"]).pin_policy == ""
    assert parse_args([]).pin_policy == ""


# -- taking the router off its rotation ------------------------------------


def gateway_transport():
    """A real gateway app, reachable over ASGI rather than a socket."""
    from control_plane.gateway.app import create_app

    from tests.test_gateway import build_deps, two_unequal_replicas

    return httpx.ASGITransport(app=create_app(build_deps(deployments=two_unequal_replicas())))


def test_the_policy_pin_puts_an_auto_model_back_on_auto():
    """The half that goes wrong quietly.

    A load test that pins a policy and cannot put it back has rewritten the
    operator's routing config as a side effect of measuring it. PUT is not the
    inverse of PUT -- restoring an auto model that way leaves an override
    behind -- so this is what DELETE /api/routing/{model} is for.
    """
    async def go():
        transport = gateway_transport()
        pin = loadtest.PolicyPin(
            "http://gw", {"content-type": "application/json"}, 5.0,
            "round_robin", transport=transport,
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
            before = (await c.get("/api/routing")).json()[0]
            applied = await pin.apply(["llama-3.3-70b"])
            during = (await c.get("/api/routing")).json()[0]
            restored = await pin.restore()
            after = (await c.get("/api/routing")).json()[0]
        return before, applied, during, restored, after

    before, applied, during, restored, after = asyncio.run(go())
    # Whatever the auto ladder picked is what has to come back, so this reads
    # it rather than naming it: the ladder's verdict is not this file's to pin.
    assert before["auto_selected"] is True
    assert any("round_robin" in line for line in applied)
    assert during["policy"] == "round_robin"
    assert during["auto_selected"] is False
    assert any("back to auto" in line for line in restored)
    assert after["auto_selected"] is True
    assert after["policy"] == before["policy"]
    assert after["auto_reason"] == before["auto_reason"]


def test_the_pin_leaves_alone_a_model_already_on_the_policy_it_wants():
    """Pinning what is already there would register an override that then has
    to be undone -- a mutation with nothing to show for it."""
    async def go():
        transport = gateway_transport()
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
            already = (await c.get("/api/routing")).json()[0]["policy"]
        pin = loadtest.PolicyPin(
            "http://gw", {"content-type": "application/json"}, 5.0,
            already, transport=transport,
        )
        return await pin.apply(["llama-3.3-70b"]), pin.saved

    notes, saved = asyncio.run(go())
    assert notes == []
    assert saved == {}


def test_restoring_is_safe_to_call_when_nothing_was_pinned():
    pin = loadtest.PolicyPin("http://gw", {}, 5.0, "round_robin")
    assert asyncio.run(pin.restore()) == []
