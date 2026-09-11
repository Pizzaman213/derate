# derate — working notes for Claude Code

Planning and orchestration for DGX Spark clusters: measures the interconnect,
derives the parallelism plan from it, refuses launches that will run out of
memory, and fronts every model behind one OpenAI endpoint.

**To look a contract up, read `docs/CONTRACTS.md`.** It is generated from the code --
every type, enum, constant, derived fact, route and environment variable -- and
a test fails when it goes stale, so it cannot be wrong in a way the code is not.

**There is no separate architecture journal any more.** `00-architecture.md`,
`ROADMAP.md`, `agents/*.md` and `research/*.md` were the scope, the reasoning,
the file-ownership map and the per-workstream briefs for the initial parallel-
agent build; the build is done, they went stale against each other, and they
were removed rather than kept as a source of superseded answers.
`README.md` is the pitch and the install path; `docs/CONTRACTS.md` is the frozen
facts. Git history carries the reasoning now. This file is the part that is
only useful while you are editing.

## Commands

```bash
python3 -m pytest -q -m "not slow"     # from the repo root. ~2 min, ~1300 tests.
cd ui && npm run typecheck             # tsc -b
cd ui && npm run build                 # tsc -b && vite build -> ui/dist
cd ui && npm run check                 # the whole UI suite. See below.
cd ui && npm run check -- --strict     # ...and a skip counts as a failure
cd ui && npm run screens               # capture ui/screens/<dest>.png, no assertions
python3 -m tests.model_sweep           # resolve every config in tests/resolver_data/
python3 -m tests.model_sweep --live    # ...or every local model the coordinator knows
python3 -m tests.model_sweep --arch    # VLLM_ARCHITECTURES against the image's registry
python3 -m tests.spec_sweep --scan --model X   # every published head, ranked, no launch
python3 -m tests.spec_sweep --smoke   # speculative acceptance: plumbing, ~1 min
python3 -m tests.spec_sweep --model Qwen/Qwen3-4B   # ...and a real measurement
```

`tests/model_sweep.py` is the model tester and `tests/unit/test_model_corpus.py`
gates the suite on it.

The corpus mode is hermetic and compares against
`tests/resolver_data/EXPECTED.json`. Drift fails in **both** directions but is
reported as two different events: a regression is a bug, an improvement is a
diff to review and `--regenerate`. It then runs the same corpus a second time
with the image's registry recorded, because a shape must not depend on which
runtime image is installed -- the mapper never asks a runtime anything, and
that pass is where it is proved rather than assumed. No image here prints
`unchecked`, never agreement.

`--arch` is the one that catches the failure this list keeps having: it diffs
`VLLM_ARCHITECTURES` against what the pinned image's registry actually holds.
A name the image loads and the table refuses is a 400 on a servable model
(`Gemma4ForConditionalGeneration`, for weeks); a name the table claims and the
image cannot load clears every gate and dies at load
(`MllamaForConditionalGeneration`). Both fail.

`--live` sweeps the coordinator's catalogue. Only a shape this build cannot
read is flagged. Provider-offered models are their own column rather than a
failure -- they are served over somebody's API and have no HuggingFace repo, so
"no local shape" is correct for them, and they are 427 of this box's 516 rows.
Gated and missing repos are counted apart too, because neither is a bug here.

The image-dependent tests are marked `slow`, so `-m "not slow"` deselects them
rather than starting a 24 GB container inside the suite. Deselected is not
skipped: the run says it did not check them.

When a contract, a route or an environment variable moves, regenerate:

```bash
python3 -m control_plane.contracts.manifest --write   # types, enums, constants, derived facts, env
python3 -m control_plane.contracts.routes --write     # every route both apps answer
python3 -m control_plane.contracts.document --write   # docs/CONTRACTS.md, from the two above
```

`tests/unit/test_contracts_manifest.py` fails when any of the three is stale, and
`tests/unit/test_single_source.py` fails when a copy of a fact stops matching the one
`control_plane/contracts/derived.py` names as canonical. Add a row there rather
than adding an assertion by hand.

`pyproject.toml` sets `pythonpath = ["."]`, so pytest must run from the root or
nothing imports.

**Bare `npx tsc --noEmit` typechecks nothing here** and exits 0 on any tree:
`ui/tsconfig.json` is `{"files": [], "references": [...]}`, so it resolves no
sources. The real gates are `npm run typecheck` (`tsc -b`) and `npx tsc -b
--force`; `npx vite build` bundles *without* typechecking, which is how you tell
your own breakage from a peer's in a shared checkout.

There is no UI test runner: typecheck plus the `*.check.mjs` verifiers are the
whole gate, and each one exists because a specific class of bug is invisible to
types. Add to them rather than trusting a green `tsc`.

`ui/check.mjs` (`npm run check`) is how they are run. It **discovers** them --
never a list, because a list stops covering a verifier the moment somebody adds
or renames one -- and it reads what each needs off a `// requires:` line at the
top of the file: `coordinator` (a gateway on `$DERATE_CHECK_ORIGIN`, default
:8088), `python`, `browser`, `fixtures $NAME`, or no line at all for the ones
that need nothing but the checkout. Absent means hermetic, which is the safe
default: a verifier that forgets to declare something fails loudly on a machine
without it rather than being skipped everywhere forever.

**A skip is never a pass.** Unmet requirements put a verifier in its own column
with the reason printed, never folded into the passes; `--strict` turns each one
into a failure, which is the mode for this box, where the coordinator *is* up.
Two verifiers used to `process.exit(0)` when they could not reach a coordinator,
which made "checked nothing" and "checked everything" the same exit code.

Writing a new one starts at the first assertion -- `src/check/harness.mjs` has
`load()` (esbuild-bundles a TS module so node can import it) and `report()`
(PASS/FAIL, the counter, the exit code). Each verifier is still a program you
can run on its own, and that is still the inner loop.

**`npm run screens` is how you look at the result.** `src/shell/screens.check.mjs`
drives the Chromium already in the Playwright cache -- nothing is downloaded --
over every destination in `state/routes.ts` and writes `ui/screens/<dest>.png`.
Under `npm run check` it also asserts: no console error, no uncaught rejection,
nothing non-2xx from the coordinator, a `#root` with height, and, against the
server's own `Redactor` primed with the real stored values, that no key is on
screen. Open the PNGs -- that is the only thing here that sees what shipped
rather than inferring it from source. Off-site failures are recorded in
`screens/console.txt` and deliberately not gated. That file currently records
`errors=0 assets=0` on every destination: the publisher avatars used to come
from huggingface.co and 429 in bulk, and `resolver/avatars.py` ended that --
the coordinator fetches each mark once ever, keeps the bytes and serves them
from `/api/publishers/<owner>/avatar`, same-origin.

**A publisher's mark is drawn by `OwnerMark` and subscribed to ONCE PER LIST.**
`owner.ts::avatarUrl` is synchronous and answers `null` until its batch lands,
so a list that does not call `useAvatars` sits on monograms for ever and reads
as a feature that does not work. The other half of the rule is why the
component may not subscribe for itself: several hundred cards each holding
their own state would re-render the whole grid several hundred times as one
batch settles. `owner.ts` stays React-free for a second reason -- `rows.check.mjs`
bundles it with `platform: 'neutral'`, so a `useState` in there breaks a
verifier rather than a screen.

## Layout

```
control_plane/contracts/   frozen. Change these in one place, announced, never
                           to unblock yourself -- the README calls this out as
                           the project's one failure mode.
control_plane/registry/    node agent, discovery, join, health, telemetry
control_plane/links/       measured interconnect bandwidth
control_plane/resolver/    HuggingFace id -> ModelShape, quantization table
control_plane/fit/         the blocking out-of-memory gate
control_plane/planner/     TP/PP/EP from measured facts
control_plane/deploy/      sparkrun adapter, lifecycle
control_plane/gateway/     the one HTTP surface: /v1, /api, and the built UI
control_plane/providers/   remote upstreams as route targets
control_plane/runtimes/    the inference servers derate ships itself. Runs
                           INSIDE a model container (docker/tts.Dockerfile),
                           never in the node image: torch and transformers are
                           its dependencies and neither is in requirements.txt.
                           Nothing else in control_plane/ imports it.
control_plane/redaction.py the key scrubber, standard library only. It lives
                           outside providers/ on purpose: every node needs it
                           and the worker path may not import that package.
                           providers/secrets.py re-exports all of it.
control_plane/logfiles.py  node.log and proxy.log, in one folder
control_plane/node.py      the real entry point: one process, role at runtime
ui/                        the screen. See ui/README.md, which is thorough.
tests/                     the suite is tests/unit/; everything beside it is not
                           collected -- the sweeps, fixtures/, resolver_data/, and
                           load/, which is a harness rather than a suite.
docs/screenshots/          every image the README shows, and one README over
                           both halves of it. The eight PNGs are promoted by hand
                           out of ui/screens/ after `npm run screens`.
                           brand/ under it is the banner and bare lockup: the
                           mark is a COPY of the one Header.tsx draws and the
                           colours are copies of tokens.css, so change either and
                           rerun `python3 docs/screenshots/brand/build.py`, which
                           derives the lockup's proportions from the header's own
                           CSS and outlines the wordmark so no font has to be
                           installed.
```

## Rules that are not style

**Routers register above the `StaticFiles` mount.** A Starlette mount at `"/"`
catches every path not matched by an *earlier* route, so an `include_router`
below it never runs and `/api/settings` quietly answers `index.html`. Same rule
for `/install.sh`, which `curl | sh` would otherwise pipe an HTML document.

**The UI's deep paths are answered by `_UIStatics.get_response`.** Screens live
in the path (`/cluster`, `/models/meta-llama/Llama-3.1-8B`) and nothing exists
on disk under them. The fallback deliberately does not apply to `/api`, `/v1`
or a missing hashed asset -- an asset 404 must stay a 404 or a broken deploy
renders as a blank page with nothing in the network log to explain it.

**Planner and fit strings are the product.** They render through `Verbatim`,
exactly as received: never truncated, re-cased or summarised. A refusal names
what to change, and rewriting it destroys the thing that made it useful.

**No API key is ever rendered.** `ui/src/api/redact.ts` scrubs responses on the
way in. Do not add an inverse.

**Live memory is an optional kwarg that degrades.** The fit gate prefers what a
node can actually hand out now and falls back to the static ceiling when there
is no reading. It never refuses for want of a live number.

**The launched container's only writable mount is the HuggingFace cache.**
`docker inspect` on a running sparkrun container: `HOME=/tmp`, user
`1000:1000`, one bind mount at `/cache/huggingface`, and `--rm`. Anything a
runtime should keep between launches has to be pointed in there --
`RuntimeSpec.cache_env` -> the recipe's `env:` block, which is the only channel
that exists, because the recipe format has no `volumes:` key. This is why vLLM
recompiled from cold on every launch until `VLLM_CACHE_ROOT` was set. Put it
beside `hub/`, never inside: `modelcache.py` measures and deletes
`hub/models--*` and would count compiled kernels as weights.

**The four slow things are not slow in the order you would guess.** Measured on
a 0.5B launch with the image and weights already local: CUDA graph capture 40s+,
python/vLLM import (twice -- APIServer, then the EngineCore fork) ~26s, sparkrun
prep and `docker run` ~19s, cold `torch.compile` 7.5s, **weight load 5.6s**. The
shard-counting progress bar covers the cheapest step. `VLLM_CACHE_ROOT` buys back
the 7.5s and not the 40s -- graph capture is not cached by it. Do not try to seed
the compile cache from `~/.cache/vllm` or `~/.cache/huggingface/vllm-cache`: the
key hashes the vLLM/torch build, the current nightly's key is in neither, and it
is 3.6 GB that will never be read. The only lever on graph capture is
`enforce_eager` or a trimmed `cudagraph_capture_sizes`, which trade decode
throughput for startup and so would have to be an option, not a default.

**A launch is four slow things, and it says which one it is on.**
`deploy/progress.py` classifies it from literal markers sparkrun and the
runtime print, and shows their line verbatim. Two rules there are load-bearing:
an unrecognised line changes nothing (the newest line is not necessarily the
current activity), and the only `fraction` that exists comes from the
checkpoint loader counting its own shards. When a marker stops matching after a
version bump, fix the marker -- do not fall back to inferring the phase.

**`--speculative-config` cannot go through `extra_args`, and that is correct.**
`_EXTRA_ARG_SAFE` (deploy/recipes.py) rejects `{`, `"` and spaces, which is
right for operator text and is why the control plane builds this one flag
itself: `flags.py::render_speculative_config` composes the JSON from an enum
member and an int, then validates the *rendered string* against a whole-string
grammar -- the thing that actually reaches `bash -c`, not the inputs. Two
further traps on the way out. The value starts with `{`, so written as a plain
YAML scalar it parses as a flow mapping and sparkrun substitutes a Python dict
repr, single quotes and all, into a command already single-quoted -- it is
`json.dumps`'d into a double-quoted scalar for that reason. And only vllm has
the flag: `speculative_refusal()` refuses sglang and tts up front, because a
launch that starts and quietly decodes one token per step while the fit gate
charged the draft's memory is the failure nobody notices.

**A speculative head can ship in its own repository, and most do.** Reading the
target's `config.json` answers what the *checkpoint* carries (MTP via
`num_nextn_predict_layers`, DSpark via `dspark_*`) and says nothing about the
ecosystem: `Qwen/Qwen3-Next-80B-A3B-Instruct` declares no MTP while the pinned
image loads `Qwen3NextMTP` from a separate repo, and there are EAGLE3 and DSpark
heads for most popular models. `speculators.py::head_option` prices one the
operator names -- it is a repo, so the same mapper sizes it and the same weight
index measures it. Three rules there: the class is PREFIX-matched (the image
registers 61 speculator classes and grows every release, so an exact table would
refuse a head the runtime can load), `hidden_size`/`vocab_size` must equal the
target's (a head reads the residual stream directly), and a head whose shards
cannot be measured is refused rather than estimated -- the analytic split for a
head is a floor and lands ~16% low, which is the wrong direction for a gate.
`imageprobe.py` keeps the speculator registry so "can this image load it" is
evidence rather than a claim.

**A `trust_remote_code` checkpoint can pass every gate and die at load.**
sparkrun runs the container with `HF_HUB_OFFLINE=1`, so transformers cannot
fetch a checkpoint's custom modeling module at startup. `katuni4ka/tiny-random-
deepseek-v3` declares an `auto_map`, and the launch failed with *"We couldn't
connect to huggingface.co ... and couldn't find them in the cached files"* --
after the architecture check passed (`DeepseekV3ForCausalLM` is in the image's
registry) and the fit gate said `fits`. Nothing here asks whether a config's
remote code is already on the box, and that is the gap. Prefer a checkpoint
with no `auto_map` when picking one to test with; `tests/spec_sweep.py`'s
`SMOKE_MODEL` says so where it chooses one.

**The scan is written down, which is what lets the checkbox recommend
anything.** A cold scan is a dozen hub searches plus a resolve per candidate --
39 for Qwen3-30B-A3B -- so as long as it was recomputed per view it had to hide
behind a button, and a button is not a recommendation. `head_scan.save_scan`
puts it under `data_dir()/scans/heads`, and the key is the model **and the
image version**: whether a head is loadable at all comes from the image's own
speculator registry, so a different image is a different answer and must MISS
rather than approximate. What is stored is priced facts and never the ceiling
-- the ceiling is arithmetic against the *serving* node's bandwidth, so a
stored one would be a number the next node invalidates.

**ngram wins the ceiling ranking by construction, and must never be the
auto-pick.** `draft_ratio` is the draft's parameters over the target's active
ones, so a draft that reads no weights has ratio 0, `speculative_overhead(k, 0)`
is exactly 1.0, and the ceiling is `base * (k + 1)` -- above ANY head with
weights, at every k, for every model. Measured on the real thing: ngram scores
11.0x for Qwen3-30B-A3B against the best EAGLE3 head's 6.9x, and on
`deepseek-ai/DeepSeek-V3` it claims 11.0x against the checkpoint's OWN MTP
module at 1.5x -- a trained mechanism the checkpoint ships, out-ranked by
arithmetic that describes no mechanism at all. So
`head_scan.recommend` drops weightless options and `WEIGHTLESS_NOTE` says why
on screen; ngram stays in the picker and stays in the ranking table, because it
is free and on repetitive input it is genuinely right. Test
`draft_params == 0`, never falsy -- `None` there means the cost was NOT derived
(checkpoint DSpark) and is a different thing entirely.

**Every tok/s the fit gate reports is PER SEQUENCE, and must say so.**
`calculator.py` computes `kv_read` at one sequence deliberately, whatever
`req.max_concurrent_seqs` says -- `predict_decode_tps` is a single-stream,
bandwidth-bound model and the cache it is handed has to match. Unlabelled, the
concurrency control moved the memory bars and left the rates alone, so a plan
sized for 16 still advertised a 6x speculative ceiling. Do not "fix" that by
passing the concurrency in: that yields a slower per-sequence number, not an
aggregate, and an aggregate needs a compute model this project does not have.
`_speculative_range` states the scope and, above one sequence, says
speculation pays most where the batch is small enough for decode to be
bandwidth-bound and that derate has not measured where that stops.

**A vision head passes every dimension check, and only its own config gives it
away.** `AngelSlim/Qwen3-VL-30B-A3B-Instruct_eagle3`,
`nvidia/Qwen3-30B-A3B-Thinking-2507-Eagle3` and `Qwen/Qwen3-30B-A3B` all report
hidden_size 2048, vocab_size 151936 and vision_params 0 -- there is no geometry
to compare. The VL head declares `target_model_type: "qwen3_vl"` and the text
head declares nothing, so `speculators.target_type_conflict` refuses on a
DECLARED MISMATCH only; absence is never a refusal, because most good heads say
nothing. Matching is on SEGMENT boundaries (`qwen3` is consistent with
`qwen3_moe`, `qwen3_vl` is not) -- a bare `startswith` accepts the VL head,
since it never sees the end of the shared `qwen3` stem. Adding the field also
needed `cache.py`'s `SCHEMA_VERSION` bumped AND the field added to its
serializer: an entry written before it existed reads back as `""`, which is
exactly the value that lets the head through.

**The scan states a MULTIPLE; only the card states tok/s.** The scan ranks
every head against `predict_decode_tps` at the node's bandwidth with no context
and no concurrency, while the Verdict card three lines below answers for the
actual plan -- so the screen was showing "up to 150 tok/s" directly above
"speculating 10 to 90 tok/s" for one head. The ratio is the part that does not
depend on the assumption: the ceiling is `base * (k+1) / overhead`, so dividing
by `base` cancels it exactly, and 150/24.7 and 90.2/15.0 are the same 6.1x.
`speculative.ts::speedup` is that division and `speculative.check.mjs` pins both
observed pairs.

**The n a control applies must be the k the number it displayed was computed
at.** The server ranks a head at its `max_tokens`; `refFor` defaulting to
`default_tokens` ticked a box advertising a ceiling at n=8 and launched at n=3.
Same class as everything else here: whatever is shown and whatever is sent are
one value or they will drift.

**The head scan is on its own route, never on `/api/plan`.** That route answers
on every keystroke in the Serve panel and a scan costs a handful of hub searches
plus a resolve per candidate. `GET /api/models/speculative-heads?model_id=` is
on demand and cached for 30 minutes in memory over the durable record above.
The field's pure decisions live in `ui/src/tabs/models/speculative.ts` --
split out because `check.mjs` bundles with esbuild's `platform: 'neutral'`, so a
verifier cannot import a module that pulls in React. That is enforcement, not
convention, and `speculative.check.mjs` is what it buys. Two traps the browser hit,
both worth keeping in mind for any control that lives inside the Verdict card:
selecting "a draft head I will name" used to send a placeholder `eagle3` with no
head, which the coordinator correctly refused, which nulled `result` -- and
since the control renders inside the card that `result` builds, **it unmounted
the instant you selected it**. A mode is not a request. And the mode itself is
local state, not `?head=`: an empty head is dropped by `href`, so the URL round
trip silently lost it.

**`--scan` finds a head; only a launch says which is best.** The hub has 84
candidates for Qwen3-4B and 78 for Qwen3-8B, so naming one by hand is how only
four ever got tried. `tests/spec_sweep.py --scan` searches, prices each through
`speculators.head_option` and ranks them, launching nothing -- it runs on a full
box. Two traps it was built around. A head is trained against the UNQUANTIZED
base and named after it, so searching `Qwen3-4B-AWQ eagle` finds 3 candidates
where `Qwen3-4B eagle` finds 84; `query_stems` strips the quant token using
`quant_detect.from_name` rather than a second list. And the name pre-filter
reads the REPOSITORY, never the org -- `taobao-mnn` publishes ordinary
safetensors heads, and matching `-mnn` against the whole id dropped three good
ones. The ranking is a CEILING, which rewards a small head with a high k and
barely separates anything within a family (nine EAGLE3 checkpoints tie at 426
tok/s), so `--measure-top` shortlists one head per METHOD FAMILY by downloads:
four launches answering four questions instead of three answering one. The
geometry gate cannot tell a VL head from a text head when the dims match --
`Qwen3-VL-4B-Instruct_eagle3` passes -- so the shortlist is a shortlist, not a
verdict.

**Acceptance is measured, not assumed, and the range stays.**
`python3 -m tests.spec_sweep` launches, drives three prompt sets from
`tests/spec_data/`, and reads vLLM's own `vllm:spec_decode_*` counters through
`control_plane/metrics_scrape.py`. The image exports
`num_accepted_tokens_per_pos`, which is CUMULATIVE acceptance per draft
position (vLLM's own dashboard divides it by `num_drafts`) -- so one launch at
k=10 answers for every k below it and `speculative_best_k` solves the curve
without relaunching. A record lands in `data_dir()/measurements/spec/`, keyed by
workload, GPU, bandwidth and image version, and a mismatch on any of those
MISSES rather than approximating. The card cites a matching record beside the
floor/ceiling range and never in place of it: the range is still what is true
for a workload nobody measured. The two ends and the measured point are the
same arithmetic -- all-accept reproduces the ceiling exactly, none-accept the
floor -- and `test_spec_measure.py` pins that, because it is the only thing
tying the new number to the old claim.

**Speculative decoding is priced, and its throughput is not claimed.** The fit
gate charges the draft's weights and its drafted positions into the existing
`weights`/`kv_cache` terms, so every refusal string and the context search work
unchanged. What is reported is a floor and a ceiling -- and the floor is BELOW
the ordinary rate whenever the draft has weights, because nothing accepted means
the draft head was read for nothing. There is no assumed acceptance rate
anywhere; derate does not measure one, and the sentence on screen says so. Do
not add a constant to turn that range into a number. DSpark is detected from
`dspark_*` and deliberately refused rather than estimated: nothing here has
derived what that module weighs, and the gate does not launch what it cannot
budget.

**The measurement that counts finally runs, and it is `nccl-torch`.**
`NcclMeasurer` has called itself that since it was written and has never once
executed here: `available()` needs `mpirun` + nccl-tests, installed nowhere, so
every link fell through to `ib_write_bw` -- raw RDMA scaled by
`IB_TO_NCCL_RATIO` with a latency for a different operation.
`TorchNcclMeasurer` runs a two-rank all_reduce INSIDE the serving image, which
beats nccl-tests as evidence because nccl-tests would measure a different NCCL
than the one that serves. Measured on this fabric: **13.2 us and 19.2 GB/s
against the estimate's 5.728**, in under five seconds, `estimated=False` so the
0.42 ratio does not apply. It sits below nccl-tests (a box that has it keeps
the reference) and above ib_write_bw (an estimate must never beat a
measurement).

**A rung that measures correctly and reports itself unavailable never runs.**
This one shipped that way for an hour: `available()` called
`runner.run(None, argv)` when the protocol is `run(argv)`, so it raised,
returned False, and the ladder would have descended to the estimate for ever
while the rung worked perfectly when called by hand. `test_available_is_true_
when_the_image_is_here` exists for that and fails with `assert False is True`
when the bug returns. Check `available()` against a real box, not only
`measure()`.

**`ib_write_bw` cannot cover two rails and must say so.** perftest takes one
`-d` per process, so on a two-HCA box its figure is about half the fabric.
Running it twice and adding would be inventing an aggregate; `_single_rail_note`
names the rail it drove instead, and the nccl-torch rung is the real fix --
NCCL binds every rail itself (`NET/IB : Using [0]rocep1s0f0 [1]roceP2p1s0f0`).

**A link's tuning is on the screen, and `calibrated` is a separate field from
`env` because the server answers `{}` for two different things.** Nobody has
measured this pair, and somebody measured it and the DEFAULT won -- collapsing
them offers to redo finished work and reads a completed calibration as a gap.
`tabs/cluster/tuning.ts` is the four-state fold (`uncalibrated` /
`default-best` / `tuned` / `failed`) and it is a plain module because
`check.mjs` bundles `platform: 'neutral'` and cannot import React;
`tuning.check.mjs` is the first verifier coverage anything in the cluster rail
has ever had. The evidence rows ride the payload too: the choice is a trade
between two regimes four decades apart, so the winner alone is an assertion and
"=4 was faster in bulk and 24% slower at decode" is the argument.

**`POST /api/links/tune` returns 202 and a thread, and the button reads the
SERVER's `measuring`.** A calibration is one collective per candidate, so it is
minutes: holding the request open would tie up a worker and trip any proxy in
front, and a LOCAL in-flight flag would clear in milliseconds and invite a
second press. That press would not be refused -- `calibrate` and `measure`
share one lock, so it would block silently for minutes. `measuring` finally
wires `LinkService.measuring()`, which `links/README.md` says exists "so the
screen can say a measurement is running" and which nothing had ever called.
It rides `link_payload`, so `/api/cluster`, `/api/topology` and `/api/links`
cannot disagree.

**Auto-calibration made a piece of UI copy false, which is the shape of bug to
expect from it.** `SelectionRail` said measuring "saturates the link for about
a minute"; once `measure()` started calibrating a never-calibrated pair off its
own back, it took several more. Saying a minute and taking five reads as a
hang. Any change to what `measure()` does now has to check that sentence.

**The interconnect calibrates itself, per pair, and the rule is `best in bulk,
no regression at decode`.** `LinkService.calibrate` times a two-rank all_reduce
under each candidate setting and stores an `NcclRecord` per pair, image and
size band (`measurements/nccl`, third instance of the `SpecRecord` pattern);
`measurements.tuning_env` picks the winner and `manager._nccl_env_for` applies
it, with `DERATE_NCCL_ENV` overriding. It fires from `measure()` only when
nothing is stored for that pair and image -- keeping the measurement path's own
discipline, "bring-up and on demand, never on a timer" -- and every pair a plan
spans must agree or nothing is applied.

**Only measured traffic may lift the decode guard, and it is lifted per
served model.** `EngineLoad.prefill_share` is this window's
`vllm:prompt_tokens_total` over the total -- a deployment serving 8k prompts
for 50-token answers is a bandwidth workload wearing a serving workload's
clothes. `measurements.preference_for` turns a stored `WorkloadRecord` into
`"bulk"` above `PREFILL_DOMINANT_SHARE` (0.8, not 0.5, because the trade is
asymmetric: ~5% more bandwidth for 24% on decode), and `tuning_env(prefer=)`
then allows the regression the balanced rule refuses.

**It cannot be per REQUEST, and the reason is worth knowing before anyone
tries.** NCCL reads its environment once at communicator init and vLLM builds
that communicator once at engine startup, so one setting serves every request
for the process's life. The next launch is the only moment the choice exists,
which is why the input is the LAST incarnation's traffic. `context_length`
would be the easy signal and is the wrong one: it is a capacity, not a
workload, and a 131k deployment may serve nothing but short prompts.

The two-size rule is not fussiness. Measured here: `NCCL_MAX_NCHANNELS=4` is
the BULK winner (18.55 GB/s at 4 MiB against 8.09) and a **decode regression**
(21.87 us at 8 KB against 17.69). A launch gets one environment covering both
regimes, so the bulk winner is the wrong answer; `=2` is faster than the
default at both and is what ships. `NCCL_NET_OVERHEAD` is absent from the
candidates because sweeping 1/5/13/25/50 moved nothing outside the repeat
noise, and `NCCL_PROTO` because forcing the small-message protocol costs 6x at
4 MiB -- NCCL already selects by size and a global override throws that away.

**Anything timing this fabric must run the container a real launch runs.**
`--device /dev/infiniband` is not a tuning choice: without it NCCL reports
`NET/IB : No device found` and drops to TCP over the management LAN with no
warning -- 243 us for an 8 KB all-reduce against 17 us over RoCE. sparkrun's
rootless branch maps it for a real launch (`Devices=[{/dev/infiniband ...}]
User=1000:1000`), so `links/collective.py` does too. And NCCL picks its own
bootstrap address: on this estate it chose a stray `10.100.0.2/30` on the
peer's RoCE NIC that the coordinator cannot route to, and hung until timeout --
which reads exactly like a fabric fault and is not one. Name the interface.

**NCCL is configured through the recipe's `env:` block, and derate ships no
defaults for it on purpose.** `DERATE_NCCL_ENV` ("NCCL_PROTO=LL,...") is parsed
per launch and rendered beside `VLLM_CACHE_ROOT`; `NCCL_TUNABLES` is every
variable the image's `libnccl.so.2` honours and `nccl_env_refusal` checks each
name against it, because **NCCL ignores an unknown variable with no warning and
no error** -- a typo would launch, report as tuned, and run the defaults. Single
-rank plans are refused rather than set: there is no communicator. The reason
there is no default value is that nothing has measured a collective on this
fabric, and NCCL already picks a protocol by size -- forcing the small-message
one would help a ~5.6 KB decode all-reduce and hurt the multi-MB prefill.
`tests/nccl_sweep.py` is what turns that into a number; put a value in
`flags.py` only with a row from it behind you.

**Read the library, not the binding.** `torch.cuda.nccl.version()` reports
**2.29.7** inside the pinned image and every `libnccl.so.2` on its disk is
**2.31.2** -- the first is what torch was compiled against, the second is what
loads and what the runtime's own error text names. `NCCL_TUNABLES` was read off
the loaded library. The same trap explains a stale finding: the IB abort
(`local access violation work queue error`) was reproduced against the HOST's
torch on NCCL 2.28.9, which is neither of those.

**vLLM cannot be told to replicate attention, so derate only prices it.**
`ALLREDUCES_PER_LAYER = 2` halves to 1 if attention is replicated instead of
sharded, and `comm.attention_replicated_is_cheap` says which checkpoints want
that. It stops there: vLLM's mechanism is `disable_tp`, a per-layer
CONSTRUCTOR argument absent from `arg_utils.py`, `config/*.py` and `envs.py`.
There is no flag and no env var, so a knob for it would be one that silently
does nothing. Note also that replication is not merely a duplicated KV cache --
every rank computes every Q head -- and that vLLM ALREADY replicates the KV
head whenever `tp_size > total_num_kv_heads` (linear.py), which a 1-KV-head
checkpoint like DeepSeek-V4-Flash always hits.

**A pipeline at concurrency 1 costs the same at every degree, exactly.**
`comm.pipeline_bubble_fraction` is `(p-1)/(m+p-1)`, so at one in-flight
request the bubble `(p-1)/p` cancels the `1/p` compute saving precisely:
DeepSeek-V3 is 135.53 ms per step at pp=1, 2, 4, 8, 16 AND 32. Adding machines
to a pipeline at single stream buys CAPACITY, never speed. Tensor parallel
halves with every doubling, which is why a huge model at low concurrency wants
TP even though TP's wire costs ~72x more -- and why the opposite is true for a
small one, where the wire dominates. The crossover moves with active
parameters, node count and concurrency, so no scalar threshold can express it;
`TP_VIABLE_THRESHOLD = 40.0` GB/s additionally sits ABOVE this hardware's
23.15 GB/s ceiling and can never be cleared on a two-Spark cluster.

**TP's exchange count does not depend on its degree, so its wire is a FLOOR.**
`num_layers * ALLREDUCES_PER_LAYER` -- 122 for DeepSeek-V3, the same at tp=2
and tp=32. At 40 us that is 4.88 ms whatever the compute behind it, so the
share grows from under a tenth of the step at tp=2 to more than half at tp=32,
and the floor rather than bandwidth is what stops TP scaling. Two levers exist
and neither is compression (measured: zlib 1.08x at 0.05 GB/s against a
13.98 GB/s bar, and at batch 1 the entire payload prize is 0.36% of a step).
Speculation amortises the floor across the accepted tokens of one step --
`estimated_step_seconds(..., speculative_window=k+1)`, payload only.
`attention_replicated_is_cheap` prices the other: sharding only the MLP halves
the count, paid for with a duplicated KV cache, which is a bad trade for dense
GQA (327,680 B/token) and a good one for MLA (70,272 B/token) -- cheapest
exactly on the huge MoE checkpoints that need the most nodes.

**`latency_us` is a COLLECTIVE, and the `ib_write_bw` rung cannot measure one.**
It reports `None` rather than offering `ib_write_lat`, which times a one-sided
2-byte RDMA write -- a different operation from a two-rank all-reduce, which
also carries a kernel launch, a reduction and a synchronisation. Reported as
the collective it recorded 1.44 us against this project's own nccl-tests
fixtures at 40.0, so the planner's most decision-sensitive input was ~28x
optimistic and every plan that turned on the wire was wrong in TP's favour.
`UNMEASURED_COLLECTIVE_LATENCY_US` is charged instead and `_Facts.latency_clause`
says so on screen. Do not "fix" the absence by substituting the write again.

**A restart budget has to survive the relaunch.** `MAX_RESTART_ATTEMPTS = 3`
bounded nothing until 2026-09-11: `restart.py::_handle` minted a fresh
`_RestartState` on every FAILED event, and `_retry_loop` returns on
`"launched"` -- meaning SUBMITTED, not READY -- so a model dying at startup got
a new budget per crash. Measured: 1,094 `launching -> failed` transitions in
two days, every gap exactly `RESTART_BACKOFF_S[0]` because the counter never
reached the second rung. The state is now reused and cleared only on READY,
which is the only evidence a relaunch worked. Giving up is announced from
`_handle`, not just `_retry_loop`, because a crash loop's last attempt exits
through the success path. The underlying launch was never retryable anyway --
`Errno 98` on a fixed port -- and `port_is_free` is local by design
(manager.py:267 says so), so the coordinator cannot see a port held on the
node it is placing on.

**`--ctx-size` is llama.cpp's SHARED pool, and derate multiplies before it
renders.** Measured on the pinned image, not read: `--ctx-size 2048 --parallel
2` prints `n_slots = 2, n_ctx_slot = 1024`, and `--ctx-size 8192 --parallel 4`
prints `n_ctx_slot = 2048`. vLLM and SGLang do the opposite -- `--max-model-len`
is per sequence -- and derate's `context_length` is the per-sequence number
because that is what the fit gate approves and the Verdict card shows. So
`RuntimeSpec.context_is_shared_pool` makes `recipes.synthesize` emit
`ctx_size = context x concurrency`, and `adopt.py` divides it back out.
Getting this wrong is the quietest failure available: nothing errors, the
launch comes up READY, and every request gets a fraction of the approved
window while the record, the card and the gate all agree with each other and
disagree with the server. Same shape as the `--kv-cache-dtype` bug below, and
found the same way -- by reading what the server printed.

**A CPU node's budget is a MEASUREMENT or it is nothing.** `addressable_memory`
still means "bytes reachable by the GPU" and is still 0 on a Pi; the `llamacpp`
runtime spends host RAM instead, and `telemetry.allocatable_bytes` grows a
`DeviceClass.CPU` branch returning MemAvailable less `config.cpu_host_reserve`
(a floor-and-fraction, because 8 GiB is ~6% of a Spark's pool and the whole of
a small Pi's). The consequence is the one rule in this project that runs
backwards: **"live memory degrades to static" does not apply here**, because
the static ceiling for a machine with no GPU is 0 and always will be. There is
nothing to degrade to, so an unsampled CPU node REFUSES and says the
measurement is missing. Degrading would mean fabricating.

**"Has no GPU" and "cannot serve" are two questions now.** They were one for
the life of the project, asked as `addressable_memory <= 0` in six places.
`RuntimeSpec.memory_pool` (`"gpu"` / `"host"`) is the fact, and
`flags.placement_refusal` is the single predicate -- both directions are
refusals, because `llamacpp` refuses a machine that HAS a GPU.

**That refusal is an ACCOUNTING one, and the tempting reason for it is the
wrong one.** "llama.cpp would not use the card" is a preference, and a
preference is not grounds for a refusal here: it would run on a Spark's CPU
perfectly well, just slowly, and nothing would break. What derate cannot do is
BUDGET it there. `registry.allocatable_bytes` branches on device class --
host memory for `DeviceClass.CPU`, a GPU figure for every other -- so the fit
gate would compare a host-RAM demand against a GPU number, which can approve a
launch that exhausts RAM as easily as refuse one that would have fitted. The
refusal therefore describes THIS BUILD and names what would have to change.
It is also wider than it strictly needs to be: on a discrete-GPU box VRAM and
host RAM are separate pools and a host-pool runtime there is perfectly
budgetable. Narrowing that is a change to `allocatable_bytes`, not to the
placement predicate. `ui/src/state/runtime.ts::
canCarryRank` is the client's mirror and `board.ts` must keep agreeing with it,
or the node board offers a tick the launch answers with 400.

**sparkrun's container defaults assume a GPU, and one of them is fatal
without one.** `EXECUTOR_DEFAULTS` carries `gpus: "all"`, which on a machine
with no NVIDIA runtime is a `docker run` that fails outright rather than a
wasted flag. The recipe's own top-level `executor_config:` block overrides it
(CLI > recipe > runtime plugin > defaults), and `""` is the documented
clear-it value -- but it must be QUOTED in the YAML, because a bare `gpus:`
parses as null, null means "not set", and "not set" falls through to the
default this exists to remove. The same block clears the image's ENTRYPOINT,
which a `sleep infinity` container cannot also run; sparkrun's own Atlas
plugin does exactly this for exactly this reason.

**sparkrun ships a real `llama-cpp` plugin, so this one is not a borrow.**
Unlike `tts`, which sets `sparkrun_runtime="vllm"` because sparkrun has never
heard of it, sparkrun's own `runtimes/llama_cpp.py` (that path is in ITS
package, not derate's -- `control_plane/runtimes/` holds only `tts.py`)
exists with `runtime_name = "llama-cpp"`, renders the recipe's `command:`
verbatim, and resolves the GGUF into the
container cache -- rewriting `-hf <repo>` to `-m <path>` on the way, which is
why `adopt.py` reads both. Its model spelling is `owner/repo:QUANT` and
derate's is `hf://owner/repo/file.gguf` (the form that names the exact blob, so
`resolver/gguf.py` can measure its tensor directory); `flags.llamacpp_model_spec`
is that seam and uses `gguf_names.quant_token`, because sparkrun globs the
cache for a filename CONTAINING the publisher's own token.

**A RESOLVED id has no scheme, and two functions tested for one.** This is the
bug the paragraph above was describing while not doing, found 2026-09-11 the
first time anything actually launched llamacpp here. `hf://owner/repo/file.gguf`
is what an operator passes; `ModelResolver.resolve_gguf_full` sets
`name = f"{repo}/{filename}"` and the scheme is GONE from `shape.model_id` --
which is exactly what `manager.launch` and `recipes.synthesize` pass on. So
`llamacpp_model_refusal` read every legitimate hub GGUF as a local path and
refused it ("that is a path to a file on this machine"), and
`llamacpp_model_spec` declined to translate and left sparkrun a three-segment
id whose `parse_gguf_model_spec` finds no colon and takes the whole string as a
repository that does not exist. One root cause, two symptoms, so one predicate
-- `flags.is_hub_gguf_blob` -- and it is narrow on purpose: the thing it must
not swallow is a genuine local path, which also ends in `.gguf` and also has
slashes. The leading sigil is the whole distinction. Test both spellings of any
id function on this path; the `hf://` one alone is what shipped.

**`llama-server` is not on PATH in the image derate defaults to.** sparkrun's
own `scitrera/dgx-spark-llama-cpp` has it at `/usr/local/bin`, which is why a
bare binary name looks right -- but derate deliberately does not use that image
(it links libcuda, and this runtime exists for machines with no CUDA). The
upstream CPU build `ghcr.io/ggml-org/llama.cpp:server` puts it at
`/app/llama-server`, as the ENTRYPOINT, NOT on PATH -- and derate then clears
that entrypoint, because a `sleep infinity` container cannot also run it. So
the rendered command died with `llama-server: not found`. Both templates carry
`PATH="/app:$PATH"` now, prepended rather than hardcoded as an absolute path,
so pointing `DERATE_LLAMACPP_IMAGE` back at sparkrun's CUDA image still works.

**The llamacpp path cannot be exercised on this estate, and that is two
constants stacking rather than a bug.** `placement_refusal` refuses a host-pool
runtime on a machine that HAS a GPU (an accounting refusal -- see above), so it
can only go to `connor-pi`; the Pi has 1.9 GB of RAM, `cpu_host_reserve` floors
at 1 GiB and `FRAMEWORK_OVERHEAD` is a flat, runtime-blind 1 GiB
(`contracts/constants.py`), so its live budget is ~348 MiB and no GGUF of any
size is ever approved. A CPU node needs ~2 GiB free before the gate will pass
anything. `FRAMEWORK_OVERHEAD` is a frozen contract and describes a CUDA
runtime, not llama.cpp; making it per-runtime is a contract change and is
therefore announced, never done to unblock a test.

**A KV cache has a WIDTH and a CAP, and both have to travel.** The fit gate
halves `kv_bytes_per_token` for `fp8` (`fit/kv.py::KV_ELEM_BYTES`), approves a
context on that basis, and hands the launch the halved byte budget through
`--kv-cache-memory-bytes`. Until 2026-09-10 the width itself went nowhere:
`_VLLM_COMMAND` had no `--kv-cache-dtype`, and `deploy/flags.py`'s own header
says a knob whose recipe_key is absent from the template is dropped in
silence. So a request for fp8 sized narrow, budgeted narrow, and then ran
fp16 -- the budget honoured, so no OOM, no error, and half the approved
context served with nothing on screen to say so. Latent only because
`settings.default_kv_dtype = "fp16"` made gate and engine agree by accident.
`render_kv_cache_dtype` now renders it and `kv_cache_dtype_refusal` refuses it
on a runtime whose template has no such flag, the way `speculative_refusal`
and `graph_capture_refusal` already do -- and this one has to refuse rather
than drop, because the caller has ALREADY been handed a verdict computed at
that width. `VLLM_KV_CACHE_DTYPES` was read off the pinned image's own
`CacheConfig`, not off a changelog; `int8` and `uint8` are priced by the gate
and cannot be served, so they raise. The invariant is in
`tests/unit/test_integration_kv.py`: a dtype the gate prices at anything other
than two bytes must produce a flag or an error, never silence.

**`gpu_memory_utilization` is a claim on the whole device, not a limit.** vLLM
refuses to start unless that share of the GPU is *free*, then fills it with KV
cache. A constant therefore asks for the same slice of the machine for a 0.5B
model as for a 120B one, and fails outright on any node with neighbours --
which is how every launch here died at startup while the fit gate's own record
said `fits`, basis `live`, 1.8 GiB into 24.0 GiB. `deploy/utilization.py`
derives it from `fit.breakdown.total`, capped by what is free and by
`DEFAULT_GUARDRAIL`. The denominator is the live sample, because that is what
the runtime divides by.

**A dead engine does not kill its container.** A solo launch execs the serve
command inside a container that sleeps forever, so `sparkrun cluster check-job`
still says "running" after the engine has exited -- which is how a launch that
died in thirty seconds was watched for the full 1800s. The runtime's own
`EngineCore failed to start.` is what catches it (`progress.py::_RUNTIME_FATAL`),
and `sparkrun logs` is the only way to see it: solo output goes to
`/tmp/sparkrun_serve.log` inside the container, not to `docker logs`. That
command *follows* -- it is `tail -f` and never returns -- so it is subscribed
to, never polled.

**Cross-node expert parallel needs sparkrun >= 0.3.7, and the box has 0.2.40.**
vLLM is not the problem and never was: handed
`--enable-expert-parallel --data-parallel-size 2`, 0.28.1rc1 parses both, starts
its DP coordinator, and serves -- `system_fingerprint` ends `-dp2-ep`, and both
ranks sit at 13007 MiB. What could not finish is the LAUNCH. sparkrun's cluster
path execs the head, waits for it to bind the torch-distributed **master port**
(`vllm_distributed.py` passes `init_port=25000, port_label="Master Port"`), and
starts the workers only after that wait returns -- while a plan with
`tp * pp == 1` never emits `--master-port` at all, so nothing binds 25000, the
wait burns 60x2s, and rank 1 is never started. The head then waits for it
forever. Upstream calls it issue #292 and fixed it in **0.3.7** via
`native_rendezvous_port` (found by bisecting the wheels; 0.3.0-0.3.6 lack it).
`data_parallel_refusal` is the gate, and it is a VERSION gate -- on 0.3.7+ the
shape launches unchanged, verified through derate's own `/v1` on 0.3.8.

**Upgrading sparkrun is cheap and the two things it breaks are both in
`deploy/`.** The cluster handle gained a second hex segment
(`sparkrun_<hex>_<hex>`), and `CLUSTER_ID_RE` anchored after the first one
matched neither version -- a launch whose containers were up on both hosts came
back "exited 0 but printed no cluster id", was recorded FAILED, and left a
workload nothing had a handle to stop. And 0.3.8 warns that the recipe's
`VLLM_CACHE_ROOT` overrides a runtime-cache mount it now manages itself, which
is a warning rather than a failure but means the compile cache lands somewhere
sparkrun does not persist; `runtime_cache:` is its suggested key. Everything
else was checked and is identical -- see `deploy/README.md` under `flags.py`.

**`DERATE_SPARKRUN_BIN` is read now.** It was declared in `envspec.py`,
published from there into `docs/CONTRACTS.md`, and named in `NOT_INSTALLED`'s
"point DERATE_SPARKRUN_BIN at it" -- while `SparkrunAdapter.__init__` defaulted
to the bare constant and nothing anywhere called `os.environ` for it. Setting it
did nothing, silently. `flags.py::sparkrun_binary` resolves it now, which is
also how a second sparkrun can be tried against the live coordinator without
touching the one on PATH that every other session shares.

**`sparkrun` and "DGX Spark" are external names.** The project renamed
`sparkplane` -> `derate` on 2026-09-07 as a hard cut, but `sparkrun` is an
external launcher (parsed with literal regexes) and "Spark" is the hardware.
Grep for `sparkplane`, never for bare `spark`.
It is **not NVIDIA's, and not a binary**, which this file said for months and
which shaped more than one decision: `sparkrun-0.2.40.dist-info/METADATA` names
scitrera.ai, `License-Expression: Apache-2.0`, `Development Status :: 3 - Alpha`,
and what installs is 44,294 lines of readable Python under
`~/.local/share/uv/tools/sparkrun/`. Read it when a regex here stops matching --
the source is right there, and every one of derate's literal expressions was
written against it rather than against a documented interface.

**GB10 unified memory: `nvidia-smi` reports aggregate memory as N/A.** Only
per-process accounting works, which is why the container runs `--pid=host`. The
static ceiling overstates what is available by roughly 10x because the OS shares
the model's pool -- never present it as headroom.

**Platform questions are answered with evidence, not `platform.system()`.**
The probe runs `nvidia-smi` and reads what comes back; `detect_bridge_networking`
looks at the interfaces; the host readers fall through when `/proc/meminfo` is
not there, not when the OS has a particular name. A name test is wrong inside a
Linux container on a Mac, under WSL, and on a hardened `/proc` — all cases where
the Linux path still works and should still win. The two exceptions are
deliberate and commented: `fsutil` tests `hasattr(os, "fchmod")`, and `procs.py`
refuses the kill verb off POSIX because `os.kill` there terminates what it is
asked to probe.

**`/data` is not a default any more.** `control_plane/paths.py::data_dir()` is
the single resolver: `DERATE_DATA_DIR`, else `/data` when writable, else the
platform's application-state directory. Eleven modules used to re-type the
fallback, and off Linux every one of them failed into a `log.warning` — which
added up to a coordinator that regenerated its cluster token on every restart.

**The logs are one folder, and `proxy.log` is not a grep of `node.log`.**
`control_plane/logfiles.py` writes both into `paths.py::logs_dir()` --
`DERATE_LOG_DIR`, else `<project root>/logs`, next to the code rather than
under `data_dir()` like everything else in `paths.py`. In the image that root
is `/opt/derate`, which is a layer and *not* the `derate:/data` volume, so a
deployment that wants logs to survive `docker rm` sets
`DERATE_LOG_DIR=/data/logs`. The narrow file is filtered at the *handler* because a
traceback's continuation lines carry no logger name: `grep gateway.proxy
node.log` keeps `forward failed` and drops the exception under it. Add a logger
to `PROXY_LOGGERS`, never a second file. An unwritable folder is a warning and
no handlers -- this never fails a startup -- and the redacting filter is on the
handler, not on a logger, because the loggers it covers are `gateway.*` and a
`logging.Filter` on a logger is never consulted for another one's records.

**Unsloth Studio is AGPL-3.0-only.** Read it as a spec, reimplement, never
vendor.

**`tts` is a runtime like the other two, and its architecture list is a claim
about `control_plane/runtimes/tts.py`.** A name belongs in
`TTS_ARCHITECTURES` once somebody has run that checkpoint through that server,
never because the model card says TTS -- the server calls exactly
`processor(...)`, `generate() -> codes`, `decode_audio(codes)`, and a
checkpoint whose remote code answers something else is a launch that clears
every gate and dies at load. Its recipe says `runtime: vllm` on purpose:
sparkrun's runtime field picks its orchestration plugin, and every plugin runs
an explicit `command:` verbatim.

## The UI's URL scheme

Added 2026-09-07. The path names the screen (`/dashboard`, `/models`,
`/cluster`, `/chat`, `/spend`, `/settings`, plus `/setup`, which is
a real destination and off the nav) and that screen's own subject
(`/models/<model id>`); the query names what is selected (`?node=`, `?link=`,
`?dep=`, `?open=node:spark-01`, `?ctx=`/`?seq=`). `ui/src/state/
routes.ts` is the scheme -- pure, verified by `router.check.mjs` --
`router.tsx` is the provider, and `selection.tsx` reads and writes it so no
call site knows a URL is involved. Selecting replaces, opening pushes. Details
and the reasoning are in `ui/README.md` under **URLs**.

This list said `/storage` and did not say `/setup`. Storage has never been a
destination -- `tabs/storage/*` is four cards composed inside `SettingsTab`
-- and a `Dest` that is not in `SEGMENT` parses as the dashboard and renders
`/undefined`, silently, which is why `router.check.mjs` now walks the whole
`Dest` union rather than a hand-kept list of paths.

`/speech` existed briefly as the only way in the product to send
`POST /v1/audio/speech`, on the argument that the chat picker filtered audio
models out and so a TTS deployment was visible everywhere and reachable from
nowhere. That filter was removed instead: the chat picker now lists every
modality, `ChatTab` posts to the endpoint the picked row advertises (text and
embedding to `/v1/chat/completions`, a speech row to `/v1/audio/speech`), and
the composer grows a voice/format pair or a file chooser to match. `/speech`
was retired on 2026-09-08 once it had nothing left that `/chat` did not
already do.

## This box

- The live coordinator is on **:8088** (`DERATE_UI_DIR=/home/connor/derate/ui/dist`,
  data in `/tmp/derate-live`). `:8080` is somebody else's. Boot test instances
  on **18xxx**.
- `npx vite build` is live on :8088 with no restart, *if* `DERATE_UI_DIR` still
  points at `ui/dist` -- read it off `/proc/<pid>/environ` and confirm with
  `curl -s localhost:8088/ | grep -o 'assets/index-[^"]*\.js'`. Equal hashes is
  the only proof.
- Server-side changes do need a restart, and the process **does not exit on
  SIGTERM**: `kill PID`, wait for the port to free, then `kill -9`. Never
  `pkill -f control_plane.node` -- the pattern matches the agent's own shell.
- **The tts image is not published, and is faked by a local tag.**
  `deploy/flags.py` defaults the `tts` runtime to
  `ghcr.io/pizzaman213/derate/tts:latest`, which does not exist in the
  registry -- five launches died on `manifest unknown` before anyone noticed.
  It is built from `docker/tts.Dockerfile` on each Spark against that node's
  local `sparkrun-eugr-vllm:latest` base (only the pip layers are new bytes)
  and then **tagged with the ghcr name locally**, which is the same trick the
  vllm-audio default relies on. `DERATE_TTS_IMAGE` also works and is one
  restart away from being lost -- it was, at 22:58 on 2026-09-07, and the next
  launch went straight back to `manifest unknown`. Prefer the tag. Publishing
  the real image is the actual fix and has not been done.
- **Peer Claude sessions edit this checkout at the same time.** Files change and
  are deleted under you; `npm run build` will fail on somebody else's in-flight
  type error in a file you never touched (`npx vite build` bundles without
  typechecking, so you can still tell your breakage from theirs). Before
  declaring UI work done, grep for a marker unique to your edit rather than
  trusting that typecheck passed.
- **Commit with explicit paths only.** A directory-scoped `git add` sweeps a
  peer's uncommitted work into your commit; it has happened more than once.




Dont commit to git or ask unless I ask`s