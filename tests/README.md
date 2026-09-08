# tests

The regression net for a system whose product is a refusal. Most of what
`derate` ships is a sentence -- a fit gate naming the term that blew the budget,
a planner naming the measurement it read, a provider service that must never
print a key -- so the assertions here are about *what was said*, not only about
what was returned. A test that checks a verdict and not its reason is not
testing this product.

Run it from the repo root. Nowhere else works:

```bash
python3 -m pytest -q -m "not slow"      # ~2 minutes
```

`pyproject.toml` sets `pythonpath = ["."]` and `testpaths = ["tests"]`, so
`control_plane` and `tests.fixtures` import only when the root is the working
directory. There is no `conftest.py` anywhere in the checkout: shared fixtures
live in `tests/fixtures/__init__.py` and shared fakes are imported from
`unit/test_gateway.py` and `unit/test_registry.py` by name, so every dependency
between test modules is a visible `import` rather than a fixture that appears
from nowhere.

**The suite is one folder down.** [`unit/`](./unit/README.md) holds the forty
pytest modules and a document over them; what stays here is everything that is
*not* collected: three scripts, the frozen fixture package, the captured
resolver corpus and the load harness. That split is the whole reason for the
subfolder -- `tests/` used to be forty test modules with four non-tests mixed
in among them, and the four read as tests that had been forgotten about.

`1,838` `def test_` functions across 40 test modules, plus three scripts and a
fixture package.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `__init__.py` | 0 | makes `tests` a package so `tests.fixtures` and `tests.load` import |
| `doc_sweep.py` | 175 | a script, not a test: checks every README against the folder it describes |
| `model_sweep.py` | 723 | a script, not a test: the model tester -- corpus, live catalogue, architecture diff |
| `tts_diagnose.py` | 483 | a script, not a test: instruments a real TTS checkpoint run |
| [`unit/`](./unit/README.md) | 36,414 | the suite: 40 modules, 1,838 tests |
| [`fixtures/`](#fixtures) | 376 | the frozen day-0 shapes, profiles and link measurements |
| [`load/`](./load/README.md) | — | the load harness: finds where the gateway breaks and derates it |
| [`resolver_data/`](#resolver_data) | 27 files | 26 captured `config.json` payloads and `EXPECTED.json` |

## `__init__.py`

Empty, and required. Without it `tests.fixtures`, `tests.load`, `tests.unit` and
`tests.model_sweep` are not importable packages, and half the suite imports one
of the four by dotted name.

## `doc_sweep.py`

Checks the folder documentation against the folders. `python3 -m tests.doc_sweep`
reports and exits 1 on any finding; `--quiet` gives the exit code alone. Three
findings exist, each a way documentation fails silently: `undocumented` (a file
with no `## Layout` row -- the reader never learns it is there), `phantom` (a row
for a deleted file), and `broken-link` (a relative link that does not resolve).

**It is deliberately not collected by pytest.** Many sessions edit this checkout
at once and CI runs the suite on every push to the default branch, so a
documentation rule collected as a test goes red on somebody else's commit that
added a module -- a cost imposed on a person who never asked for it. Promoting it
is one wrapper away and the docstring writes it out. `ROW_NAME` only accepts a
first column that matches `FILENAME`, because a Layout table's first column also
carries expressions and type names and treating `frozenset({...})` as a filename
reports a phantom for every one. Prose is not checked; no script can tell whether
a paragraph is true.

## `model_sweep.py`

The model tester, and a script rather than a test. Three modes, three questions.

`--corpus` (the default) resolves every `resolver_data/*.config.json` and
compares against `EXPECTED.json`. Hermetic, about a second, and it is what
`test_model_corpus.py` gates the suite on. `Drift` splits disagreement by
direction on purpose: **a regression is a bug to fix, an improvement is a diff to
review and `--regenerate`**, and both stop a green build because a change nobody
looked at is what this guards against. `compare_probed` then runs the same corpus
a second time with the image's registry recorded, because a shape must not depend
on which runtime image is installed -- the mapper never asks a runtime anything,
and that pass is where it is proved rather than assumed.

`--arch` is `architecture_divergence`: `support.VLLM_ARCHITECTURES` diffed
against what the pinned image's registry actually holds. A name the image loads
and the table refuses was `Gemma4ForConditionalGeneration` returning a 400 on a
servable model for weeks.

`--live` walks the coordinator's own catalogue through `origin()` and resolves
each row. **What counts as a failure is deliberately narrow**: only a model this
build cannot read a shape out of. A model that resolves and is then refused by
every runtime is reported and not flagged -- that is the support table doing its
job. `_HUB_FAULT` is the literal-substring list that separates a gated repo, a
404 and a network wobble from a genuine miss, kept as literals because the
resolver's sentences are the product and are not getting error codes just for
this.

## `tts_diagnose.py`

Instruments a real run of the speech runtime's checkpoint, to characterise why
the voice-cloned path occasionally collapses -- ending after a couple of frames
instead of speaking the sentence. Phase 0 needs only the tokenizer and runs in
under a second; phases 1-5 load the full checkpoint through the same
`AutoProcessor`/`AutoModel.from_pretrained(..., trust_remote_code=True)` call
`control_plane.runtimes.tts.SpeechEngine.load()` makes, and never touch this
project's HTTP surface. Phase 6 (`--phase 6 --trials 30`, opt-in and slow) calls
the real `SpeechEngine.synthesize()`.

It ruled out a codec bug, a reference-length-accounting bug and a
prompt-position bug; found and fixed a separate real defect -- an unregistered
`<|speaker:0|>` tag fragmenting into garbage tokens on every conditioned prompt,
now `_patch_reference_text_tag` in `control_plane/runtimes/tts.py`; and via a
39-trial sweep established that the collapse is rare and sentence-dependent
rather than a broken prompt. The default `--max-new-tokens 32` is a sanity
budget, not a reproduction: pass 512 for numbers comparable to a deployment.

## `fixtures/`

`fixtures/__init__.py` (376 lines) is the shared day-0 fixture set, written
alongside the contracts so every component tests against the same numbers.
**Frozen: extend by adding, never by editing an existing value**, or two modules'
tests disagree about the same object.

Four model shapes chosen to cover four different arithmetic paths: `LLAMA_3_3_70B`
(dense, GQA -- 64 query heads over 8 KV heads), `GPT_OSS_120B` (MoE, mxfp4,
sliding window, 201,088 vocab), `QWEN3_30B_A3B` (MoE that fits one node) and
`DEEPSEEK_V3` (MLA), plus `AUDIO8_TTS_0_6B`. Three node profiles: `SPARK_01` and
`SPARK_02` are GB10s built from `GB10_ADDRESSABLE`/`GB10_TOTAL_MEMORY`/
`GB10_MEM_BANDWIDTH` rather than typed figures, and `WS_3090` is the discrete
card. `MODEL_SHAPES`, `NODE_PROFILES`, `NODE_STATES` and `GB10_PROFILES` are the
collections.

Four link measurements, and the set is an argument rather than a list.
`LINK_SPARK_10G` is the real one -- 10.2 GB/s all-reduce, 9.0 sendrecv, 40 µs,
`gpudirect_rdma=False`, method `nccl-tests`. `LINK_SPARK_FAST` is the
counterfactual: a driver update enables GPUDirect RDMA, bandwidth roughly doubles
past `TP_VIABLE_THRESHOLD`, and the planner's answer must flip to tensor parallel
on its own -- that pair is what makes
`test_answer_flips_when_only_the_measurement_changes` a proof rather than an
assertion. `LINK_SPARK_ETH_3090` is heterogeneous pairing over the management LAN
at 1.1 GB/s, two orders of magnitude down from the ConnectX-7 path, so the planner
must not pool those nodes by default. `LINK_SPARK_STALE` is eight days old, past
the seven-day window: the planner may still use it, the UI marks it, and the
figures are otherwise identical to `LINK_SPARK_10G` so the only variable is age.
`LINKS` is keyed by the *sorted* node pair, which is how link records persist.

The builders -- `node_state`, `pp2_plan`, `single_node_plan`, `fits`,
`wont_fit`, `fits_degraded` -- are hand-built stand-ins for the planner's and the
fit gate's output, so a downstream test does not need either component to exist.

## `resolver_data/`

26 captured `config.json` payloads and `EXPECTED.json`, the golden resolved
output, keyed by the same 26 names. The configs are the shapes that have been
hard rather than a sample: MLA (`deepseek-v3`, `deepseek-v4-flash`), mxfp4 and
sliding window (`gpt-oss-120b`, `gpt-oss-20b`), MoE (`mixtral-8x7b`,
`qwen3-30b-a3b`), five quantization encodings (`llama-3.1-8b-awq`,
`llama-3.1-8b-fp8-ct`, `nvfp4-llama-70b`, `qwen2.5-7b-gptq`,
`qwen3-next-80b-nvfp4`), composite configs (`qwen2.5-vl-7b`,
`diffusiongemma-26b`, `qwen3-omni-30b-a3b`) and speech (`whisper-large-v3`,
`parakeet-tdt-0.6b`, `kokoro-82m`, `audio8-tts-preview-0.6b`,
`qwen3-tts-custom-voice`).

**Two of them are supposed to be unreadable** -- a CTranslate2 export and a static
embedding model, neither of which has a transformer in it -- and `compare_corpus`
says so explicitly, because a sweep that flagged them would cry wolf on every run.
`EXPECTED.json` records the resolved outcome plus the `_FIT_FIELDS` subset of the
mapped shape, and refusals are stored through `_refusal_key`, which keeps the
stable half of the sentence: the full refusal carries a searched-count and a field
list meant for a person, and pinning it whole would make the expectations churn on
wording. The folder is excluded from `doc_sweep.py`'s `SKIP_DIRS` walk for exactly
that reason -- it is captured data, documented as a whole rather than file by file.

## `load/`

[`load/`](./load/README.md) is a harness, not a suite. It finds where the gateway
breaks and produces its derating curve -- `python -m tests.load calibrate`, then
`rps`, `stream`, `probes`. It is never collected by `pytest tests/`;
`test_load.py` is the three-assertion SLO guard derived from what it measured, and
that file is `slow`.

## Failure behaviour

- **No `sparkrun`.** `needs_sparkrun` skips those tests; everything else in
  `test_deploy.py` runs against `FakeAdapter`.
- **No `docker`.** `needs_docker` skips. The image-build test additionally checks
  for `docker buildx` and skips when QEMU emulation is absent, naming what to run
  instead.
- **No network.** `test_resolver.py` probes the hub once and marks the whole
  network half `needs_hub`; `DERATE_TEST_NETWORK=0` skips it without probing.
- **No runtime image.** `TestAgainstTheImage` and `model_sweep --arch` report that
  they could not look. `architecture_divergence` and `compare_probed` return
  `None` rather than an empty result that reads as agreement.
- **No coordinator.** `model_sweep --live` needs one on `origin()`; the corpus
  mode needs nothing but the checkout.
- **A stall.** Peer sessions run the full suite in this checkout too. A run that
  hangs is usually contention for the same ports and the same
  `~/.cache/huggingface`, not a deadlock in the code under test.
- **A test that fails on a file you never touched.** Files here change under you.
  Check `git status` before assuming your edit caused it.

## Deliberately not built

**A documentation test.** `doc_sweep.py` is a script on purpose. Its docstring
gives the four-line wrapper that would promote it and argues against running it:
in a checkout many sessions edit at once, a documentation rule collected as a test
goes red on somebody else's commit.

**A `conftest.py`.** Shared state is imported by name from
`tests/fixtures/__init__.py`, `test_gateway.py` and `test_registry.py`, so every
dependency between modules is visible at the top of the file that has it.

**A wider net in `model_sweep`.** Only a model this build cannot read a shape out
of counts as a failure. Flagging models that resolve and are then refused by every
runtime would bury the one signal that means derate has a bug under a list of
checkpoints that were never servable.

**A second assertion for a derived fact.** `test_single_source.py` says it:
extend it by adding a row to `control_plane/contracts/derived.py`, not by adding
an assertion here.
