"""The text-to-speech runtime: everything about it that does not need a GPU.

`control_plane/runtimes/tts.py` runs inside the model container, where torch,
transformers, soundfile and scipy live. None of those are in requirements.txt,
so the module keeps them behind function-local imports and everything below
holds on a machine that has none of them -- which is the point: the refusals
this server writes are the part a person reads, and they must be testable
without hardware.

What is NOT covered here, and cannot be: that the checkpoint loads and speaks.
That was verified by running the server against
Audio8/Audio8-TTS-Preview-0.6b on a GB10 and playing the result.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_plane.runtimes import tts  # noqa: E402


def wav_bytes(seconds: float = 1.0, rate: int = 44100) -> bytes:
    """A real, minimal, mono 16-bit WAV. Built by hand so this file needs no
    encoder: the runtime reads clip durations with libsndfile when it is there,
    and the fixture has to be something libsndfile will actually open."""
    frames = int(seconds * rate)
    data = b"\x00\x00" * frames
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


def wav(path: Path, seconds: float = 1.0, rate: int = 44100) -> None:
    path.write_bytes(wav_bytes(seconds, rate))


# ── the default voice library ────────────────────────────────────────────────


def fake_rows(speaker, text, src="https://example.invalid/clip.wav"):
    return {"rows": [{"row": {"speaker_id": speaker, "text_normalized": text,
                              "audio": [{"src": src}]}}]}


def fetchers(*, rows, clip=None, fail_json=None, fail_bytes=None):
    """A stand-in for the two network calls, so every test below is hermetic.

    The real ones are one-line urllib wrappers, split out of
    `ensure_default_voices` precisely so this file never reaches the network:
    a suite that fetches a dataset to check a naming rule is a suite that goes
    red when somebody else's CDN does.
    """
    seen = []

    def fetch_json(url, timeout):
        seen.append(url)
        if fail_json:
            raise fail_json
        return rows(len(seen) - 1) if callable(rows) else rows

    def fetch_bytes(url, timeout, limit):
        if fail_bytes:
            raise fail_bytes
        # A real RIFF, because load_voices measures the clip with libsndfile
        # before it will accept it -- so a placeholder here would test the
        # rejection path and call it success.
        return wav_bytes(2.0, 24000) if clip is None else clip

    return fetch_json, fetch_bytes, seen


def test_an_empty_directory_gets_a_starter_library(tmp_path):
    """The point of the feature: a fresh node can clone a voice without
    anybody hand-copying a wav onto it."""
    speakers = ["2902", "1462", "6241"]
    rows = lambda i: fake_rows(speakers[i], "A sentence long enough to be a "
                                            "usable reference clip for cloning.")
    fetch_json, fetch_bytes, seen = fetchers(rows=rows)
    notes = tts.ensure_default_voices(tmp_path, fetch_json=fetch_json,
                                      fetch_bytes=fetch_bytes)
    assert len(seen) == len(tts.DEFAULT_VOICE_OFFSETS)
    installed = sorted(p.stem for p in tmp_path.glob("*.wav"))
    assert installed == ["libritts-1462", "libritts-2902", "libritts-6241"]
    # And the pairs satisfy load_voices' own contract, which is the only
    # definition of "installed" that matters.
    library = tts.load_voices(tmp_path)
    assert sorted(library.voices) == installed
    assert library.skipped == []
    assert any("installed" in n for n in notes)


def test_the_transcript_is_written_beside_the_clip_and_is_what_conditions_it(tmp_path):
    text = "If the gods have deserted their oracles, they have not deserted us."
    fetch_json, fetch_bytes, _ = fetchers(rows=fake_rows("2902", text))
    tts.ensure_default_voices(tmp_path, fetch_json=fetch_json, fetch_bytes=fetch_bytes)
    assert (tmp_path / "libritts-2902.txt").read_text(encoding="utf-8").strip() == text


def test_an_existing_library_is_never_touched(tmp_path):
    """An operator who installed their own voices has said what they want.
    Adding three strangers beside them would be the surprising thing -- and it
    is also what makes this idempotent per node."""
    wav(tmp_path / "mine.wav")
    (tmp_path / "mine.txt").write_text("my own reference clip", encoding="utf-8")
    fetch_json, fetch_bytes, seen = fetchers(rows=fake_rows("2902", "x" * 80))
    notes = tts.ensure_default_voices(tmp_path, fetch_json=fetch_json,
                                      fetch_bytes=fetch_bytes)
    assert seen == [], "it went to the network with a library already present"
    assert sorted(p.stem for p in tmp_path.glob("*.wav")) == ["mine"]
    assert "leaving the library alone" in notes[0]


def test_a_download_failure_is_a_note_and_never_an_exception(tmp_path):
    """`main` wraps parse-load-build in a try that prints FATAL_MARKER and
    ends the launch. An exception escaping here would turn a slow dataset API
    into a failed deployment, and no starter voice is worth that."""
    fetch_json, fetch_bytes, _ = fetchers(rows=fake_rows("2902", "x" * 80),
                                          fail_bytes=OSError("connection reset"))
    notes = tts.ensure_default_voices(tmp_path, fetch_json=fetch_json,
                                      fetch_bytes=fetch_bytes)
    assert list(tmp_path.glob("*.wav")) == []
    assert all("connection reset" in n for n in notes)
    assert tts.load_voices(tmp_path).voices == {}


def test_nothing_escapes_even_when_the_fetcher_is_broken_outright(tmp_path):
    """The outer guard, tested through a failure the per-row handler cannot
    see: a fetcher that is not callable at all."""
    notes = tts.ensure_default_voices(tmp_path, fetch_json=None, fetch_bytes=None)
    assert notes and all(isinstance(n, str) for n in notes)


def test_a_clip_over_the_cap_is_refused_rather_than_written(tmp_path):
    """The bound is real: the reference is packed into the same window the
    text and the generated audio share."""
    fetch_json, fetch_bytes, _ = fetchers(
        rows=fake_rows("2902", "x" * 80),
        fail_bytes=ValueError(f"clip is over {tts.MAX_DEFAULT_VOICE_BYTES} bytes"),
    )
    notes = tts.ensure_default_voices(tmp_path, fetch_json=fetch_json,
                                      fetch_bytes=fetch_bytes)
    assert list(tmp_path.glob("*.wav")) == []
    assert any("over" in n for n in notes)


def test_a_transcript_that_is_too_short_or_too_long_is_not_a_voice(tmp_path):
    """Too short and there is not enough of the voice in it to clone; too long
    and it eats the window. Checked before anything is downloaded."""
    for text in ("Hello.", "y" * (tts.DEFAULT_VOICE_MAX_CHARS + 1)):
        fetch_json, fetch_bytes, _ = fetchers(rows=fake_rows("2902", text))
        notes = tts.ensure_default_voices(tmp_path, fetch_json=fetch_json,
                                          fetch_bytes=fetch_bytes)
        assert list(tmp_path.glob("*.wav")) == []
        assert all("no usable row" in n for n in notes)


def test_the_same_reader_is_not_installed_twice_under_two_names(tmp_path):
    """Offsets are pinned to land on different readers, but the split can
    shift. Three copies of one voice is not a library."""
    fetch_json, fetch_bytes, _ = fetchers(rows=fake_rows("2902", "x" * 80))
    tts.ensure_default_voices(tmp_path, fetch_json=fetch_json, fetch_bytes=fetch_bytes)
    assert sorted(p.stem for p in tmp_path.glob("*.wav")) == ["libritts-2902"]


def test_the_licence_is_recorded_beside_the_clips(tmp_path):
    """LibriTTS is CC BY 4.0, and a directory of anonymous wav files is
    exactly where attribution gets lost."""
    fetch_json, fetch_bytes, _ = fetchers(rows=fake_rows("2902", "x" * 80))
    tts.ensure_default_voices(tmp_path, fetch_json=fetch_json, fetch_bytes=fetch_bytes)
    text = (tmp_path / tts.ATTRIBUTION_NAME).read_text(encoding="utf-8")
    assert "CC BY 4.0" in text and "LibriTTS" in text


def test_switching_it_off_leaves_the_directory_exactly_as_found(tmp_path):
    notes = tts.ensure_default_voices(tmp_path, enabled=False)
    assert list(tmp_path.iterdir()) == []
    assert "switched off" in notes[0]
    assert tts.build_parser().parse_args(
        ["--model", "m", "--no-default-voices"]
    ).default_voices is False


def test_the_default_voices_are_named_after_the_reader_not_prettified():
    """A friendly invented name would be a small lie about a real LibriVox
    reader, and it detaches a cloned voice from whose voice it is."""
    assert tts.VOICE_NAME_PREFIX == "libritts-"


# ── response formats ─────────────────────────────────────────────────────────


def test_the_default_format_is_the_one_openai_clients_send():
    assert tts.resolve_format(None).name == "mp3"
    assert tts.resolve_format("").name == "mp3"
    assert tts.resolve_format("WAV").name == "wav"


def test_aac_is_refused_by_name_rather_than_served_as_something_else():
    """A client that asked for AAC and got MP3 under `Content-Type: audio/aac`
    fails somewhere much further from here."""
    with pytest.raises(ValueError) as excinfo:
        tts.resolve_format("aac")
    message = str(excinfo.value)
    assert "cannot write AAC" in message
    # The refusal names what would have worked, which is the whole difference
    # between it and "unsupported format".
    assert "flac, mp3, opus, pcm, wav" in message


def test_an_unknown_format_lists_the_ones_that_exist():
    with pytest.raises(ValueError) as excinfo:
        tts.resolve_format("aiff")
    assert "flac, mp3, opus, pcm, wav" in str(excinfo.value)


def test_only_opus_asks_for_a_resample():
    """libsndfile's Opus writer takes 8/12/16/24/48 kHz and these checkpoints
    emit 44.1 kHz, so opus is the one format that cannot be written as it
    stands. Everything else must be left alone -- a resample nobody asked for
    is a quality loss nobody can see."""
    assert tts.FORMATS["opus"].rates and 48000 in tts.FORMATS["opus"].rates
    for name in ("wav", "mp3", "flac", "pcm"):
        assert tts.FORMATS[name].rates == ()


# ── voices ───────────────────────────────────────────────────────────────────


def test_a_clip_without_its_transcript_is_not_offered(tmp_path):
    """These models clone from a reference clip AND the words in it. A clip
    with no transcript does not clone badly, it conditions on a lie -- so it is
    skipped, and the skip says why rather than the voice merely being absent."""
    wav(tmp_path / "narrator.wav")
    (tmp_path / "narrator.txt").write_text("The exact words in the clip.")
    wav(tmp_path / "orphan.wav")

    library = tts.load_voices(tmp_path)
    assert library.names == ["narrator"]
    assert any("orphan.wav" in note for note in library.skipped)
    assert library.voices["narrator"].transcript == "The exact words in the clip."


def test_an_empty_transcript_is_skipped_too(tmp_path):
    wav(tmp_path / "quiet.wav")
    (tmp_path / "quiet.txt").write_text("   \n")
    library = tts.load_voices(tmp_path)
    assert library.names == []
    assert any("quiet.txt is empty" in note for note in library.skipped)


def test_a_reference_longer_than_the_window_is_refused_not_truncated(tmp_path):
    """The reference shares the model's 2048 packed positions with the text and
    the speech being generated. A two-minute clip does not clone badly; it
    leaves no room to speak at all."""
    pytest.importorskip("soundfile")
    wav(tmp_path / "epic.wav", seconds=tts.MAX_REFERENCE_SECONDS + 5)
    (tmp_path / "epic.txt").write_text("Far too much of it.")
    library = tts.load_voices(tmp_path)
    assert library.names == []
    assert any("over the" in note for note in library.skipped)


def test_no_voice_directory_is_an_empty_library_not_an_error(tmp_path):
    assert tts.load_voices(None).names == []
    assert tts.load_voices(tmp_path / "nope").names == []


def test_omitting_the_voice_asks_for_the_models_own(tmp_path):
    """A real request, not an error: these checkpoints generate with no
    reference at all, which is what somebody who just wants speech wants."""
    library = tts.load_voices(None)
    assert library.resolve(None) is None
    assert library.resolve("") is None


def test_an_unknown_voice_is_refused_rather_than_quietly_substituted(tmp_path):
    """The caller asked for a specific voice. Returning a different one under a
    200 is the failure they cannot see."""
    wav(tmp_path / "narrator.wav")
    (tmp_path / "narrator.txt").write_text("Words.")
    library = tts.load_voices(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        library.resolve("alloy")
    message = str(excinfo.value)
    assert "Installed: narrator" in message
    assert "Omit `voice`" in message

    # With no library at all the advice is different, because "installed:
    # nothing" would leave the reader with no next move.
    with pytest.raises(ValueError) as excinfo:
        tts.load_voices(None).resolve("alloy")
    assert "--voice-dir" in str(excinfo.value)


# ── request validation ───────────────────────────────────────────────────────


def test_a_request_for_another_model_is_refused():
    """One process holds one checkpoint. Answering for a name it does not serve
    would make a misrouted request look like a working one."""
    with pytest.raises(ValueError) as excinfo:
        tts.parse_speech_request({"model": "gpt-4", "input": "hi"}, "audio8-tts")
    assert "serves 'audio8-tts'" in str(excinfo.value)


def test_speed_is_refused_with_the_reason():
    """Resampling is not time-stretching: it moves the pitch. A 1.5x that
    quietly returned a chipmunk would be worse than this."""
    with pytest.raises(ValueError) as excinfo:
        tts.parse_speech_request(
            {"model": "m", "input": "hi", "speed": 1.5}, "m"
        )
    assert "no time-stretch" in str(excinfo.value)
    # 1.0 is what every client sends by default and must not be refused.
    assert tts.parse_speech_request({"model": "m", "input": "hi", "speed": 1}, "m")


@pytest.mark.parametrize(
    "body, expected",
    [
        ({}, "'model' parameter"),
        ({"model": "m"}, "'input' to speak"),
        ({"model": "m", "input": "   "}, "'input' to speak"),
        ({"model": "m", "input": "hi", "voice": 3}, "'voice' must be a string"),
        ({"model": "m", "input": "hi", "temperature": 0}, "greater than zero"),
        ({"model": "m", "input": "hi", "top_p": "warm"}, "must be a number"),
    ],
)
def test_every_refusal_names_the_field(body, expected):
    with pytest.raises(ValueError) as excinfo:
        tts.parse_speech_request(body, "m")
    assert expected in str(excinfo.value)


def test_a_valid_request_carries_its_sampling_through():
    parsed = tts.parse_speech_request(
        {
            "model": "m",
            "input": "Hello.",
            "response_format": "flac",
            "temperature": 0.8,
            "top_p": 0.95,
        },
        "m",
    )
    assert (parsed.fmt.name, parsed.temperature, parsed.top_p) == ("flac", 0.8, 0.95)
    assert parsed.max_new_tokens is None  # the model's own default


# ── the command line ─────────────────────────────────────────────────────────


def test_sharding_flags_are_consumed_in_order_to_refuse_them():
    """render_command emits every knob for every runtime, so a flag the server
    ignored would be a planner decision that evaporates. These two are read,
    and reading them means refusing anything but 1."""
    parser = tts.build_parser()
    args = parser.parse_args(["--model", "x/y", "--tensor-parallel-size", "2"])
    with pytest.raises(SystemExit) as excinfo:
        tts.config_from_args(args)
    assert "cannot shard" in str(excinfo.value)

    args = parser.parse_args(["--model", "x/y", "--pipeline-parallel-size", "2"])
    with pytest.raises(SystemExit):
        tts.config_from_args(args)


def test_the_served_name_falls_back_to_the_model_id():
    config = tts.config_from_args(
        tts.build_parser().parse_args(["--model", "Audio8/Audio8-TTS-Preview-0.6b"])
    )
    assert config.served_model_name == "Audio8/Audio8-TTS-Preview-0.6b"
    assert config.tensor_parallel_size == 1


def test_the_recipes_own_command_line_parses():
    """The exact argv `deploy/flags.py` renders. If this file and that template
    ever disagree the launch dies at argument parsing, minutes after the
    machines were committed."""
    from control_plane.deploy.flags import runtime_spec

    template = runtime_spec("tts").command_template
    argv: list[str] = []
    for token in template.replace("\\\n", " ").split():
        if token in ("python3", "-m", "control_plane.runtimes.tts"):
            continue
        argv.append("1" if token.startswith("{") else token)
    config = tts.config_from_args(tts.build_parser().parse_args(argv))
    assert config.trust_remote_code is True
    assert config.max_num_seqs == 1


# ── admission ────────────────────────────────────────────────────────────────


def test_admission_is_counted_not_queued():
    """One generation runs at a time whatever this says. The bound is on how
    many callers may be waiting, so the answer past it is a 503 the gateway's
    parking lot understands rather than a wait until the client gives up."""
    admission = tts.Admission(2)
    assert admission.try_enter() and admission.try_enter()
    assert not admission.try_enter()
    admission.leave()
    assert admission.try_enter()
    assert admission.in_flight == 2


def test_a_zero_limit_still_admits_one():
    """A recipe that sent 0 would otherwise refuse every request while looking
    perfectly healthy."""
    assert tts.Admission(0).try_enter()


# ── the routes ───────────────────────────────────────────────────────────────


class _StubEngine:
    """Enough of a SpeechEngine for the routes. Never synthesises: every case
    below is refused before the GPU would be reached."""

    def __init__(self) -> None:
        self.config = tts.ServerConfig(model="x/y", served_model_name="audio8-tts")
        self.voices = tts.VoiceLibrary()
        self.sample_rate = 44100


def _client():
    from starlette.testclient import TestClient

    return TestClient(tts.build_app(_StubEngine(), tts.Admission(1)))


def test_the_two_paths_the_health_probe_walks_both_answer():
    """`deploy/health.py` probes /health and then /v1/models, in that order. A
    runtime answering neither never leaves LAUNCHING."""
    client = _client()
    assert client.get("/health").status_code == 200
    body = client.get("/v1/models").json()
    assert body["data"][0]["id"] == "audio8-tts"
    assert body["data"][0]["modality"] == "speech"


def test_a_bad_request_is_a_400_and_never_a_422():
    """The regression this file exists for as much as any refusal.

    FastAPI resolves a route's annotations against the endpoint's __globals__.
    Import `Request` inside the app factory -- which is this project's
    convention everywhere else -- and with `from __future__ import
    annotations` the parameter is silently demoted to a required query field:
    every call comes back 422 with `{'loc': ['query','request']}`, and nothing
    in a typecheck or a unit test that does not open the route can see it.
    """
    reply = _client().post("/v1/audio/speech", json={"model": "audio8-tts"})
    assert reply.status_code == 400, reply.text
    assert "'input' to speak" in reply.json()["error"]["message"]


def test_the_voice_listing_says_what_was_skipped():
    """Not an OpenAI route: its voices are fixed and a cloning deployment's are
    not, so there has to be a way to ask."""
    reply = _client().get("/v1/audio/voices")
    assert reply.status_code == 200
    assert reply.json() == {"object": "list", "data": [], "skipped": []}
