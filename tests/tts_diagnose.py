"""Instrument a real run of the TTS runtime's checkpoint to characterize why
the voice-cloned path occasionally collapses -- ending after a couple of
frames instead of speaking the sentence.

00-architecture.md's "voice-cloned" appendix measured this on three reference
clips, once each, and got three collapses -- and left the cause open. This
script is what finally instrumented a real run: it found and ruled out a
codec bug, a reference-length-accounting bug, and a prompt-position bug (all
clean); found and fixed a real, separate defect (an unregistered `<|speaker:0|>`
tag fragmenting into garbage tokens on every conditioned prompt -- see
`_patch_reference_text_tag` in `control_plane/runtimes/tts.py`); and, via a
39-trial sweep through the real `SpeechEngine`, found the collapse itself is
rare (roughly a percent or few, not "every time") and sentence-dependent, not
a broken prompt. See 00-architecture.md's 2026-09-08 appendix for the full
writeup. What is still open -- *why* certain generation steps for certain
sentences get an elevated chance of sampling end-of-speech -- needs either a
much larger sweep or reading the DualAR loop's own internals; phase 6 below is
the tool for picking that back up.

    python3 -m tests.tts_diagnose                       # phases 0-5, a fresh
                                                          # LibriTTS reference
    python3 -m tests.tts_diagnose --voice-dir DIR        # reuse an installed one
    python3 -m tests.tts_diagnose --phase 6 --trials 30  # measure the collapse
                                                          # rate (opt in, slow)

Phase 0 needs only the tokenizer and runs in under a second. Phases 1-5 load
the full checkpoint onto the GPU (or CPU, slower) via `AutoProcessor`/
`AutoModel.from_pretrained(..., trust_remote_code=True)` -- the same call
`control_plane.runtimes.tts.SpeechEngine.load()` makes -- and never go through
this project's own HTTP surface at all. Their default `--max-new-tokens 32` is
a cheap sanity budget, not a faithful reproduction: pass `--max-new-tokens 512`
(production's own default) for numbers comparable to a real deployment. Phase
6 always calls the real `SpeechEngine.synthesize()` at its own default budget.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control_plane.runtimes import tts  # noqa: E402

DEFAULT_MODEL = "Audio8/Audio8-TTS-Preview-0.6b"
DEFAULT_SENTENCE = "The link is measured, not assumed."


def _load_voice(voice_dir: str | None) -> "tts.Voice":
    """A real installed voice if one is given or already on disk, else a
    freshly fetched LibriTTS pair -- the same production code path
    `ensure_default_voices` runs on every real launch, never a synthetic
    fixture (the test suite's own `wav_bytes()` writes silence, which would
    prove nothing about a collapse this is trying to explain)."""
    if voice_dir:
        library = tts.load_voices(voice_dir)
        if not library.voices:
            raise SystemExit(f"no usable voice at {voice_dir}: {library.skipped}")
        name = library.names[0]
        print(f"using installed voice {name!r} from {voice_dir}")
        return library.voices[name]

    scratch = Path(tempfile.mkdtemp(prefix="tts-diagnose-voice-"))
    notes = tts.ensure_default_voices(scratch)
    for note in notes:
        print(f"ensure_default_voices: {note}")
    library = tts.load_voices(scratch)
    if not library.voices:
        raise SystemExit(f"could not fetch a reference voice into {scratch}")
    name = library.names[0]
    print(f"fetched voice {name!r} into {scratch}")
    return library.voices[name]


# ── phase 0: tokenization, no GPU ───────────────────────────────────────────


def phase0_tokenization(model_id: str, reference_text: str) -> None:
    """Does `<|speaker:0|>` -- which `ArkttsProcessor._format_reference_text`
    injects on every reference-conditioned request and only on that path --
    actually round-trip as one token in this checkpoint's vocabulary, the way
    `<|im_start|>`, `<|voice|>` and every `<|semantic:N|>` do?"""
    from transformers import AutoTokenizer  # noqa: PLC0415

    tokenizer = AutoTokenizer.from_pretrained(
        model_id, use_fast=True, trust_remote_code=True
    )

    print("\n=== phase 0: tokenization ===")
    for probe in ("<|speaker:0|>", "<|semantic:0|>", "<|im_start|>", "<|voice|>"):
        ids = tokenizer.encode(probe, add_special_tokens=False)
        atomic = len(ids) == 1
        print(f"  {probe!r:16s} -> {ids}  {'atomic' if atomic else 'FRAGMENTED'}")

    tagged = f"<|speaker:0|>{reference_text}"
    plain_ids = tokenizer.encode(reference_text, add_special_tokens=False)
    tagged_ids = tokenizer.encode(tagged, add_special_tokens=False)
    extra = len(tagged_ids) - len(plain_ids)
    print(f"  reference text alone: {len(plain_ids)} tokens")
    print(f"  with the injected tag: {len(tagged_ids)} tokens ({extra:+d})")
    if extra > 1:
        print(
            f"  -> the tag fragments into {extra} garbage tokens spliced "
            f"immediately before the real reference text, conditioned path only"
        )


# ── phases 1-5: the loaded checkpoint ───────────────────────────────────────


def _load_model(model_id: str):
    import torch  # noqa: PLC0415
    from transformers import AutoModel, AutoProcessor  # noqa: PLC0415

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    print(f"loading {model_id} on {device} ({dtype})...")

    processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    model = (
        AutoModel.from_pretrained(model_id, trust_remote_code=True, dtype=dtype)
        .eval()
        .to(device)
    )
    loader = getattr(model, "load_codec", None)
    if callable(loader):
        loader(device=device)
    return processor, model, device


def phase1_length_accounting(model, processor, voice: "tts.Voice") -> None:
    import soundfile as sf  # noqa: PLC0415
    import torch  # noqa: PLC0415

    print("\n=== phase 1: reference length accounting ===")
    info = sf.info(str(voice.audio_path))
    batch = processor(
        text=[DEFAULT_SENTENCE],
        reference_audio=[str(voice.audio_path)],
        reference_text=[voice.transcript],
        return_tensors="pt",
    )
    device = next(model.parameters()).device
    audio_values = batch["reference_audio_values"].to(device)
    audio_lengths = batch["reference_audio_lengths"].to(device)
    with torch.inference_mode():
        codes, code_lengths = model.encode_audio(audio_values, audio_lengths)
    frame_length = model._arktts_codec.frame_length
    sample_rate = model._arktts_codec.sample_rate
    expected = round(info.duration * sample_rate / frame_length)
    actual = int(code_lengths[0])
    print(f"  true duration:       {info.duration:.3f}s")
    print(f"  frame_length/sr:     {frame_length} samples @ {sample_rate}Hz")
    print(f"  expected frames:     {expected}")
    print(f"  code_lengths[0]:     {actual}")
    print(f"  {'OK, matches' if abs(actual - expected) <= 1 else 'MISMATCH'}")


def phase2_codec_roundtrip(model, processor, voice: "tts.Voice", out_dir: Path) -> None:
    import soundfile as sf  # noqa: PLC0415
    import torch  # noqa: PLC0415

    print("\n=== phase 2: codec round-trip (isolates the codec from the DualAR loop) ===")
    batch = processor(
        text=[DEFAULT_SENTENCE],
        reference_audio=[str(voice.audio_path)],
        reference_text=[voice.transcript],
        return_tensors="pt",
    )
    device = next(model.parameters()).device
    audio_values = batch["reference_audio_values"].to(device)
    audio_lengths = batch["reference_audio_lengths"].to(device)
    with torch.inference_mode():
        codes, code_lengths = model.encode_audio(audio_values, audio_lengths)
        waveforms, lengths = model.decode_audio(codes)
    samples = waveforms[0, : int(lengths[0])].float().cpu().numpy()
    out_path = out_dir / "phase2_roundtrip.wav"
    sf.write(out_path, samples, model.config.codec_sample_rate)
    duration = len(samples) / model.config.codec_sample_rate
    print(f"  wrote {out_path} ({duration:.2f}s, reference was {sf.info(str(voice.audio_path)).duration:.2f}s)")
    print(f"  listen to it, or run Whisper over it, and compare to {voice.audio_path}")


def phase3_prompt_boundaries(model, processor, voice: "tts.Voice") -> None:
    import torch  # noqa: PLC0415

    print("\n=== phase 3: prompt boundary bookkeeping ===")
    batch = processor(
        text=[DEFAULT_SENTENCE],
        reference_audio=[str(voice.audio_path)],
        reference_text=[voice.transcript],
        return_tensors="pt",
    )
    device = next(model.parameters()).device
    batch = {k: v.to(device) for k, v in batch.items() if hasattr(v, "to")}
    with torch.inference_mode():
        prompt, prompt_mask = model._prepare_prompt(**batch)
    position_ids = prompt_mask.cumsum(-1).sub(1).clamp_min(0)
    prefix_len = int(batch["prefix_attention_mask"].sum())
    print(f"  prompt shape:        {tuple(prompt.shape)}")
    print(f"  prompt_mask sum:     {int(prompt_mask.sum())} / {prompt_mask.shape[-1]} (should be equal, batch size 1)")
    print(f"  prefix length:       {prefix_len}")
    window = position_ids[0, max(prefix_len - 3, 0) : prefix_len + 3]
    print(f"  position_ids around prefix boundary: {window.tolist()}")
    diffs = (position_ids[0, 1:] - position_ids[0, :-1]).tolist()
    bad = [i for i, d in enumerate(diffs) if d != 1]
    print(f"  non-monotonic steps (should be empty): {bad[:10]}{' ...' if len(bad) > 10 else ''}")


def _trace_generation(model, batch, *, max_new_tokens: int, seed: int, verbose: bool = True):
    """Instance-level monkeypatch of the loaded model's bound methods, fully
    reversible -- never a fork of the vendored file, just an observation."""
    import sys as _sys  # noqa: PLC0415
    import torch  # noqa: PLC0415

    raw_logits: list = []
    sampled: list = []

    original_slow_step = model._slow_step

    def traced_slow_step(*args, **kwargs):
        logits, hidden = original_slow_step(*args, **kwargs)
        raw_logits.append(logits.detach().clone())
        return logits, hidden

    original_sample_semantic = model._sample_semantic

    def traced_sample_semantic(*args, **kwargs):
        result = original_sample_semantic(*args, **kwargs)
        sampled.append(result.detach().clone())
        return result

    model._slow_step = traced_slow_step
    model._sample_semantic = traced_sample_semantic
    try:
        generator = torch.Generator(device=next(model.parameters()).device).manual_seed(seed)
        with torch.inference_mode():
            codes = model.generate(
                **batch,
                max_new_tokens=max_new_tokens,
                temperature=None,
                top_p=None,
                do_sample=True,
                generator=generator,
            )
    finally:
        model._slow_step = original_slow_step
        model._sample_semantic = original_sample_semantic

    arktts = _sys.modules[type(model).__module__]
    mask = arktts.ArkttsSemanticLogitsProcessor(
        model.config.semantic_begin_id, model.config.semantic_end_id, model.config.eos_token_id
    )
    frames_emitted = int((codes[0, 0] >= 0).sum()) if codes.numel() else 0
    if verbose:
        print(f"  frames emitted: {frames_emitted} / {max_new_tokens} requested (seed={seed})")
    for step, logits in enumerate(raw_logits[: max_new_tokens + 1]):
        masked = mask(None, logits.clone())
        probs = torch.softmax(masked.float(), dim=-1)
        eos_prob = probs[0, model.config.eos_token_id].item()
        got = int(sampled[step][0]) if step < len(sampled) else None
        if step == 0:
            step0_eos_prob = eos_prob
        if verbose:
            top5 = torch.topk(probs[0], 5)
            marker = " <- EOS" if got == model.config.eos_token_id else ""
            print(
                f"  step {step:2d}  eos_prob={eos_prob:.4f}  "
                f"top5={list(zip(top5.indices.tolist(), [round(v, 3) for v in top5.values.tolist()]))}  "
                f"sampled={got}{marker}"
            )
    return frames_emitted, step0_eos_prob if raw_logits else None


def _trace_trials(model, batch, *, max_new_tokens: int, trials: int, base_seed: int) -> list[int]:
    """Repeat the same conditioned/unconditioned prompt under different seeds.

    A single seed can get lucky or unlucky: the prior investigation's own
    server log showed the *same* sentence and voice producing 0.28s, 3.95s,
    1.39s, 1.11s... across repeated identical calls -- a spread far too wide
    to be sampling noise on a healthy prompt, and a strong hint that this is a
    per-step probability that is *elevated but not 1.0*, not a hard-coded
    early return. `step0_eos_prob` is included because it is seed-invariant
    (no sampling has happened yet at step 0), so it isolates the raw model
    belief from the coin flip on top of it.
    """
    frame_counts = []
    step0 = None
    for trial in range(trials):
        frames, s0 = _trace_generation(
            model, batch, max_new_tokens=max_new_tokens, seed=base_seed + trial, verbose=(trial == 0)
        )
        frame_counts.append(frames)
        if trial == 0:
            step0 = s0
    print(f"  step-0 eos_prob (seed-invariant): {step0:.4f}" if step0 is not None else "  (no steps ran)")
    print(f"  frames emitted across {trials} trial(s): {frame_counts}")
    return frame_counts


def phase4_generation_trace(model, processor, voice: "tts.Voice", *, max_new_tokens: int, seed: int, trials: int) -> None:
    print("\n=== phase 4: per-step logits trace, conditioned vs. unconditioned ===")
    device = next(model.parameters()).device

    print("-- unconditioned --")
    unconditioned = processor(text=[DEFAULT_SENTENCE], return_tensors="pt")
    unconditioned = {k: v.to(device) for k, v in unconditioned.items() if hasattr(v, "to")}
    _trace_trials(model, unconditioned, max_new_tokens=max_new_tokens, trials=trials, base_seed=seed)

    print("-- conditioned --")
    conditioned = processor(
        text=[DEFAULT_SENTENCE],
        reference_audio=[str(voice.audio_path)],
        reference_text=[voice.transcript],
        return_tensors="pt",
    )
    conditioned = {k: v.to(device) for k, v in conditioned.items() if hasattr(v, "to")}
    _trace_trials(model, conditioned, max_new_tokens=max_new_tokens, trials=trials, base_seed=seed)


def phase5_tag_ab(model, processor, voice: "tts.Voice", *, max_new_tokens: int, seed: int, trials: int) -> None:
    """The A/B that directly tests the headline finding: does removing the
    unregistered `<|speaker:N|>` tag change how quickly EOS gets sampled?

    Uses several trials per side (not one) because the prior investigation's
    own log showed the same conditioned request producing wildly different
    durations run to run -- a probabilistic bias toward early EOS, not a
    hard-coded early return, so a single sample of either side could mislead."""
    print("\n=== phase 5: speaker-tag A/B ===")
    device = next(model.parameters()).device
    processor_cls = type(processor)
    original = processor_cls._format_reference_text

    print("-- conditioned, WITH the injected tag (today's behavior) --")
    batch = processor(
        text=[DEFAULT_SENTENCE],
        reference_audio=[str(voice.audio_path)],
        reference_text=[voice.transcript],
        return_tensors="pt",
    )
    batch = {k: v.to(device) for k, v in batch.items() if hasattr(v, "to")}
    with_tag = _trace_trials(model, batch, max_new_tokens=max_new_tokens, trials=trials, base_seed=seed)

    print("-- conditioned, WITHOUT the tag (patched) --")
    clean_text = sys.modules[processor_cls.__module__]._clean_text
    processor_cls._format_reference_text = staticmethod(clean_text)
    try:
        batch = processor(
            text=[DEFAULT_SENTENCE],
            reference_audio=[str(voice.audio_path)],
            reference_text=[voice.transcript],
            return_tensors="pt",
        )
        batch = {k: v.to(device) for k, v in batch.items() if hasattr(v, "to")}
        without_tag = _trace_trials(model, batch, max_new_tokens=max_new_tokens, trials=trials, base_seed=seed)
    finally:
        processor_cls._format_reference_text = original

    print(f"\n  frames emitted with tag:    {with_tag}")
    print(f"  frames emitted without tag: {without_tag}")
    if sum(without_tag) > sum(with_tag) * 2 and max(without_tag) > 4:
        print("  -> removing the tag materially delays EOS: confirms the headline finding")
    else:
        print("  -> no material change: the tag is not (solely) responsible, keep looking")


def phase6_collapse_rate(model_id: str, voice_dir: Path, voice_name: str, *, sentence: str, trials: int) -> None:
    """Reproduce the collapse through the real `SpeechEngine.synthesize()` --
    not a hand-rolled stand-in -- and run enough trials to measure a *rate*.

    Phase 5's single-digit trial counts couldn't tell a rare-but-real defect
    (say, a 15% chance of collapsing) from noise. This calls the actual
    production class derate ships, unseeded exactly like a live deployment,
    many times, once with today's behavior and once with the speaker-tag
    injection neutralized, and compares how often each collapses -- using the
    same collapse threshold the architecture doc's own worst cases (0.09s,
    0.28s, 0.37s, all under 0.4s) suggest: under 0.5s for a sentence whose
    genuine renditions run several seconds.
    """
    print("\n=== phase 6: collapse rate via the real SpeechEngine, many trials ===")
    config = tts.ServerConfig(
        model=model_id,
        served_model_name=model_id,
        trust_remote_code=True,
        voice_dir=str(voice_dir),
        default_voices=False,
        device="cuda",
    )
    engine = tts.SpeechEngine(config)
    engine.load()
    voice = engine.voices.resolve(voice_name)
    if voice is None:
        raise SystemExit(f"voice {voice_name!r} not found in {voice_dir}: {engine.voices.names}")

    processor_cls = type(engine.processor)
    original = processor_cls._format_reference_text
    clean_text = sys.modules[processor_cls.__module__]._clean_text

    def run(label: str) -> list[float]:
        durations = []
        for trial in range(trials):
            samples = engine.synthesize(sentence, voice)
            duration = len(samples) / engine.sample_rate
            durations.append(duration)
            print(f"  [{label}] trial {trial}: {duration:.3f}s")
        return durations

    print(f"-- WITH the injected <|speaker:0|> tag (today's behavior), {trials} trials --")
    with_tag = run("with-tag")

    print(f"-- WITHOUT the tag (patched), {trials} trials --")
    processor_cls._format_reference_text = staticmethod(clean_text)
    try:
        without_tag = run("without-tag")
    finally:
        processor_cls._format_reference_text = original

    threshold = 0.5
    with_collapsed = sum(1 for d in with_tag if d < threshold)
    without_collapsed = sum(1 for d in without_tag if d < threshold)
    print(f"\n  with tag:    {with_tag}")
    print(f"  without tag: {without_tag}")
    print(f"  collapsed (<{threshold}s): with-tag {with_collapsed}/{trials}, without-tag {without_collapsed}/{trials}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--voice-dir", default=None, help="reuse an installed voice instead of fetching one")
    parser.add_argument("--sentence", default=DEFAULT_SENTENCE)
    parser.add_argument("--max-new-tokens", type=int, default=32)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--trials", type=int, default=5, help="repeat phase 4/5 under this many seeds")
    parser.add_argument(
        "--phase", type=int, action="append", default=None,
        help="run only these phases (repeatable); default is all of 0-5 (6 is opt-in, it's slow)",
    )
    parser.add_argument("--out-dir", default=None, help="where phase 2 writes its wav (default: a scratch dir)")
    args = parser.parse_args(argv)

    phases = set(args.phase) if args.phase else {0, 1, 2, 3, 4, 5}
    out_dir = Path(args.out_dir) if args.out_dir else Path(tempfile.mkdtemp(prefix="tts-diagnose-out-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    voice = None
    if phases - {0}:
        voice = _load_voice(args.voice_dir)

    if 6 in phases:
        voice_dir = Path(args.voice_dir) if args.voice_dir else voice.audio_path.parent
        phase6_collapse_rate(args.model, voice_dir, voice.name, sentence=args.sentence, trials=args.trials)
        phases.discard(6)

    if 0 in phases:
        phase0_tokenization(args.model, voice.transcript if voice else "The exact transcript of the reference recording.")

    if phases - {0}:
        processor, model, device = _load_model(args.model)

        if 1 in phases:
            phase1_length_accounting(model, processor, voice)
        if 2 in phases:
            phase2_codec_roundtrip(model, processor, voice, out_dir)
        if 3 in phases:
            phase3_prompt_boundaries(model, processor, voice)
        if 4 in phases:
            phase4_generation_trace(
                model, processor, voice,
                max_new_tokens=args.max_new_tokens, seed=args.seed, trials=args.trials,
            )
        if 5 in phases:
            phase5_tag_ab(
                model, processor, voice,
                max_new_tokens=args.max_new_tokens, seed=args.seed, trials=args.trials,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
