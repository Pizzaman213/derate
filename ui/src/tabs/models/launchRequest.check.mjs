// `buildLaunchRequest` and `knobsFromRequest` undo each other.
//
//   node src/tabs/models/launchRequest.check.mjs
//
// A knob one side forgets is a setting a saved profile, or a "Roll back to
// this", silently drops -- the same bug `restart.py` had server-side, where a
// crash relaunch lost kv_dtype, dtype, speculative and five more.

import { load, report } from '../../check/harness.mjs'

const M = await load(import.meta.url, './launchRequest.ts')
const { check, done } = report()

const eq = (a, b) => JSON.stringify(sortKeys(a)) === JSON.stringify(sortKeys(b))
function sortKeys(v) {
  if (Array.isArray(v)) return v.map(sortKeys)
  if (v && typeof v === 'object') return Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortKeys(v[k])]))
  return v
}

const ladder = {
  variants: [
    { repo_id: 'Qwen/Qwen3-8B', gguf_file: null, label: 'BF16' },
    { repo_id: 'Qwen/Qwen3-8B-GGUF', gguf_file: 'Qwen3-8B-Q4_K_M.gguf', label: 'Q4_K_M' },
  ],
}

const full = {
  model_id: 'Qwen/Qwen3-8B',
  context: 32768,
  concurrency: 8,
  target: 'cluster',
  runtime: 'vllm',
  node_ids: ['spark-01', 'spark-02'],
  parallelism: { tensor_parallel: 2, pipeline_parallel: 1, expert_parallel: 1, data_parallel: 1 },
  extra_args: '--max-num-seqs 8 --disable-log-requests',
  served_name: 'qwen-fast',
  cudagraph_capture_sizes: [1, 2, 4, 8],
  kv_dtype: 'fp8',
  dtype: 'fp8',
  speculative: { method: 'eagle3', num_speculative_tokens: 4, model: 'org/Qwen3-8B_eagle3' },
  speculative_tokens: 2,
  trust_remote_code: true,
  engine_options: {
    reasoning_parser: 'qwen3',
    tool_call_parser: 'hermes',
    prefix_caching: false,
    max_num_batched_tokens: 8192,
    async_scheduling: true,
    limit_mm_per_prompt: { image: 2 },
    rope_factor: 2,
    generation_overrides: { temperature: 0.6, top_k: 20 },
    env: { NCCL_MAX_NCHANNELS: '2' },
  },
  lifecycle: { idle_unload_s: 3600, priority: 'high', start_at_boot: true },
}

const roundTrip = (req) => {
  const back = M.knobsFromRequest(req, ladder)
  const fields = M.routeFields(back.route, { context: 0, concurrency: 0 })
  return { back, built: M.buildLaunchRequest({ ...back.knobs, ...fields }) }
}

{
  const { back, built } = roundTrip(full)
  check(eq(built, full), 'every field round-trips through knobs and the URL')
  if (!eq(built, full)) console.log(JSON.stringify(sortKeys(built)), '\n', JSON.stringify(sortKeys(full)))
  check(back.variant?.label === 'BF16', 'model_id selects the ladder row whose launchId equals it')
  check(back.route.tp === 2 && back.route.on.join(',') === 'spark-01,spark-02', 'degrees and machines go to the URL')
  check(back.knobs.envRows.length === 1 && !('env' in back.knobs.engineOptions), 'env becomes rows, not a typed knob')
}

{
  const gguf = { ...full, model_id: 'hf://Qwen/Qwen3-8B-GGUF/Qwen3-8B-Q4_K_M.gguf' }
  check(roundTrip(gguf).back.variant?.label === 'Q4_K_M', 'a GGUF launch id selects its file row')
  check(roundTrip({ ...full, model_id: 'other/x' }).back.variant === null, 'an id no row launches selects none')
}

{
  const custom = {
    model_id: 'Qwen/Qwen3-8B', context: 4096, concurrency: 1, target: 'cluster', runtime: 'vllm',
    custom_command: 'vllm serve Qwen/Qwen3-8B --max-model-len 4096',
  }
  check(eq(roundTrip(custom).built, custom), 'a custom command round-trips alone')
  const knobs = M.knobsFromRequest(full, ladder).knobs
  const mixed = M.buildLaunchRequest({
    ...knobs, ...M.routeFields(M.knobsFromRequest(full, ladder).route, { context: 0, concurrency: 0 }),
    customCommand: true, customCommandText: 'vllm serve x',
  })
  const leaked = ['extra_args', 'kv_dtype', 'dtype', 'speculative', 'engine_options', 'cudagraph_capture_sizes']
    .filter((k) => k in mixed)
  check(leaked.length === 0, `nothing a custom command replaces is sent beside it (leaked: ${leaked.join(',') || 'none'})`)
  const ticked = M.buildLaunchRequest({ ...knobs, ...M.routeFields(M.knobsFromRequest(full, ladder).route, { context: 0, concurrency: 0 }), customCommand: true, customCommandText: '  ' })
  check(!('custom_command' in ticked) && 'extra_args' in ticked, 'a ticked but empty custom command is not in use')
}

{
  const minimal = { model_id: 'Qwen/Qwen3-8B', context: 8192, concurrency: 4, target: 'cluster', runtime: 'vllm' }
  const { built } = roundTrip(minimal)
  check(eq(built, minimal), 'defaults send no keys: absent, never null')
  check(Object.values(built).every((v) => v !== null), 'no null values on the wire')
}

{
  const k = M.knobsFromRequest(full, ladder).knobs
  const f = M.routeFields(M.knobsFromRequest(full, ladder).route, { context: 0, concurrency: 0 })
  const eager = M.buildLaunchRequest({ ...k, ...f, enforceEager: true })
  check(eager.enforce_eager === true && !('cudagraph_capture_sizes' in eager), 'eager mode suppresses trimmed graph sizes')
  const granted = M.buildLaunchRequest({ ...k, ...f, grants: ['allow_over_live_memory'] })
  check(granted.allow_over_live_memory === true, 'a granted gate is sent as true')
  check(!('allow_over_live_memory' in M.knobsFromRequest(granted, ladder).knobs), 'grants never become a saved knob')
  const blankEnv = M.buildLaunchRequest({ ...k, ...f, engineOptions: {}, envRows: [{ k: ' ', v: 'x' }] })
  check(!('engine_options' in blankEnv), 'a nameless env row and no typed knobs send no engine_options')
}

{
  check(M.routeFields({ context: null, concurrency: null, on: null, tp: null, pp: null, ep: null, spec: { method: 'eagle3', tokens: 3, model: '' } }, { context: 1, concurrency: 2 }).speculative === null,
    'a head picked but not yet typed is not a request')
  check(M.flagsToArgs(M.argsToFlags('--a 1 --b --c x')) === '--a 1 --b --c x', 'flag rows round-trip')
}

// The verdict card must judge the launch the button sends. The plan body was
// built inline without `runtime`, so /api/plan priced every engine as vllm
// (a llama.cpp GGUF answered 'Fits' on a GB10 the node board refused).
{
  const k = M.knobsFromRequest(full, ladder).knobs
  const f = M.routeFields(M.knobsFromRequest(full, ladder).route, { context: 0, concurrency: 0 })
  const shared = ['model_id', 'target', 'runtime', 'context', 'concurrency', 'node_ids', 'parallelism',
    'kv_dtype', 'dtype', 'speculative', 'speculative_tokens', 'engine_options']
  for (const runtime of ['vllm', 'sglang', 'tts', 'llamacpp']) {
    for (const custom of [false, true]) {
      const s = { ...k, ...f, runtime, customCommand: custom, customCommandText: custom ? 'serve x' : '' }
      const plan = M.buildPlanRequest(s)
      const launch = M.buildLaunchRequest(s)
      check(plan.runtime === runtime && launch.runtime === runtime,
        `${runtime}${custom ? ' (custom)' : ''}: plan and launch carry the same runtime (plan: ${plan.runtime})`)
      const off = shared.filter((key) => JSON.stringify(plan[key]) !== JSON.stringify(launch[key]))
      check(off.length === 0, `${runtime}${custom ? ' (custom)' : ''}: sizing fields agree (differ: ${off.join(',') || 'none'})`)
    }
  }
  const open = M.buildPlanRequest({ ...k, ...f, context: 0, concurrency: 0 })
  check(!('context' in open) && !('concurrency' in open), 'unset context/concurrency stay absent: the planner picks')
}

done()
