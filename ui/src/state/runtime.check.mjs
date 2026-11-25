// The runtime picker's rules, which types cannot check.
//
// `state/runtime.ts` answers four questions -- which runtime to preselect,
// which pool it spends, whether a given machine can carry a rank of it, and
// whether it shards -- and every one of them is a boolean or a string union
// that typechecks whatever it returns. The compiler is satisfied by a
// `runtimeFor` that always says `vllm` and by a `canCarryRank` that always
// says true; both would be silently wrong on exactly the cluster this file
// exists for, which is the one with no GPU in it.
//
// The case that cannot be tested any other way here: THIS box has GPUs. A
// GPU-less cluster is the whole subject and there is no hardware to reach it
// with, so the only way it is ever exercised is as a fixture.
//
//   node src/state/runtime.check.mjs
//
// Hermetic: no `// requires:` line, because nothing below reaches a network,
// a coordinator or a python interpreter. `runtime.ts` imports one `type`, so
// it bundles under `platform: 'neutral'` -- and that is enforcement rather
// than convention. The day somebody reaches for React in there, this breaks.

import { load, report } from '../check/harness.mjs'

const R = await load(import.meta.url, './runtime.ts')
const { check, done } = report()

const gpu = { profile: { addressable_memory: 120 * 1024 ** 3, device_class: 'gb10' } }
const cpu = { profile: { addressable_memory: 0, device_class: 'cpu' } }
// An RTX 3090 Ti: GPU memory of its own AND host RAM of its own.
const discrete = { profile: { addressable_memory: 24 * 1024 ** 3, device_class: 'discrete' } }

// ── the picker's own consistency ────────────────────────────────────────────
//
// A union member with no option row is invisible in the picker while being
// perfectly valid everywhere else -- the exact bug a five-entry hand-kept list
// invites, and one no compiler sees.
const OPTIONS = R.RUNTIME_OPTIONS.map((o) => o.value)
const KNOWN = ['vllm', 'sglang', 'tts', 'llamacpp', 'ollama']
for (const name of KNOWN) {
  check(OPTIONS.includes(name), `${name} has a row in the picker`)
}
check(OPTIONS.length === KNOWN.length, 'and the picker offers nothing this file does not know about')
check(new Set(OPTIONS).size === OPTIONS.length, 'no runtime is offered twice')
check(R.RUNTIME_OPTIONS.every((o) => o.label.trim().length > 0), 'every option has a label to show')

// `asRuntime` is the narrowing used for anything arriving from outside the
// type system. A new member it does not accept round-trips to vllm, silently.
for (const name of KNOWN) {
  check(R.asRuntime(name) === name, `asRuntime keeps ${name}`)
}
check(R.asRuntime('nonsense') === 'vllm', 'and falls back rather than returning null')

// ── which pool, and which machines ──────────────────────────────────────────
check(R.memoryPool('llamacpp') === 'host', 'llamacpp spends host memory')
for (const name of ['vllm', 'sglang', 'tts']) {
  check(R.memoryPool(name) === 'gpu', `${name} spends GPU memory`)
}

// The mirror of `deploy/flags.py::placement_refusal`. Both directions are
// refusals, which is the part a single `addressable_memory > 0` test cannot
// express: the Spark is refused by llamacpp and the Pi by the other three,
// while a discrete-GPU box is the one machine both kinds can carry.
check(R.canCarryRank('vllm', gpu.profile) === true, 'vllm takes a machine with GPU memory')
check(R.canCarryRank('vllm', cpu.profile) === false, 'and refuses one without')
check(R.canCarryRank('llamacpp', cpu.profile) === true, 'llamacpp takes a machine with no GPU')
check(R.canCarryRank('llamacpp', gpu.profile) === false,
  'and refuses a unified-memory one, whose host RAM is the GPU\'s pool and is budgeted as GPU memory')
check(R.canCarryRank('llamacpp', { ...gpu.profile, device_class: 'apple' }) === false,
  'Apple silicon is unified too, so it is refused for the same reason')
check(R.canCarryRank('llamacpp', discrete.profile) === true,
  'a discrete-GPU box takes llamacpp: its host RAM is a separate pool the gate budgets on its own')
check(R.canCarryRank('vllm', discrete.profile) === true, 'and still takes vllm against its VRAM')
check(R.canCarryRank('llamacpp', { addressable_memory: 0, device_class: 'unknown' }) === false,
  'an unidentified machine is refused: nothing says whether its host RAM is its own pool')

// ── the recommendation ──────────────────────────────────────────────────────
check(R.runtimeFor('speech', [gpu]).runtime === 'tts', 'a speech model picks tts')
check(R.runtimeFor('speech', [cpu]).runtime === 'tts',
  'and still does on a CPU-only cluster: modality is a requirement, not a preference')
check(R.runtimeFor('speech', [gpu]).because === null,
  'a requirement needs no explanation on screen; it is not a choice that could have gone otherwise')

check(R.runtimeFor('text', [gpu]).runtime === 'vllm', 'a text model on a GPU cluster picks vllm')
check(R.runtimeFor('text', [gpu, cpu]).runtime === 'vllm',
  'and still does on a MIXED cluster -- one GPU is enough, and this is the case a naive any/every gets backwards')
check(R.runtimeFor('text', [cpu]).runtime === 'llamacpp',
  'a text model on a cluster with no GPU at all recommends llamacpp')
check(R.runtimeFor('text', [cpu, cpu]).runtime === 'llamacpp', 'however many CPU machines there are')

// A control that moved on its own has to say what moved it.
const rec = R.runtimeFor('text', [cpu])
check(typeof rec.because === 'string' && rec.because.length > 0,
  'the recommendation carries a reason to put on screen')
check(/gpu/i.test(rec.because) && /llamacpp/i.test(rec.because),
  'and it names both the fact and the runtime, not just one')
check(R.runtimeFor('text', [gpu]).because === null,
  'the default carries none, so the note appears exactly when something was recommended')

// An empty roster is the cluster not having loaded, NOT a cluster with no GPU.
// Getting this wrong flips the picker to CPU for one frame on every page load,
// on every machine, which is worse than never moving at all.
check(R.runtimeFor('text', []).runtime === 'vllm', 'an empty node list recommends nothing')
check(R.runtimeFor('text', undefined).runtime === 'vllm', 'and neither does an absent one')
check(R.runtimeFor('text', []).because === null, 'and says nothing, having decided nothing')

// ── sharding ────────────────────────────────────────────────────────────────
check(R.shardsAcrossNodes('vllm') === true && R.shardsAcrossNodes('sglang') === true,
  'the two runtimes that split a model across ranks offer the degree fields')
check(R.shardsAcrossNodes('tts') === false, 'tts is one process holding one checkpoint')
check(R.shardsAcrossNodes('llamacpp') === false,
  'and llamacpp is refused above one rank by `shards=False`, so the fields are hidden rather than shown and rejected')
check(R.shardsAcrossNodes('ollama') === false, 'a provider has no ranks to speak of')

// ── formats ─────────────────────────────────────────────────────────────────
const vllmRow = { name: 'vllm', display_name: 'vLLM', memory_pool: 'gpu', formats: ['safetensors', 'gguf'], image: 'x', image_present: true }
check(R.formatsOf('vllm', [vllmRow]).join(',') === 'safetensors,gguf',
  'formatsOf answers what /api/runtimes says, native format first')
check(R.formatsOf('llamacpp', null).join(',') === 'gguf', 'while loading, llamacpp reads gguf')
check(R.formatsOf('vllm', null).join(',') === 'safetensors', 'and vllm defaults to an unprobed image: safetensors only')

// ── the launcher line ───────────────────────────────────────────────────────
// The single predicate the plan call, the node board, the degrees, the verdict
// and Serve itself all branch on. llamacpp is on the cluster side of it, which
// is the entire difference between it and the ollama option beside it in the
// picker -- both say "(CPU)" and only one of them is launched from here.
for (const name of ['vllm', 'sglang', 'tts', 'llamacpp']) {
  check(R.servesOnCluster(name) === true, `${name} goes through the launcher`)
}
check(R.servesOnCluster('ollama') === false, 'and ollama does not')

done()
