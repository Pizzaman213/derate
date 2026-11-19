// Verifier for the launch stepper's rules. There is no test runner in this
// repo -- typecheck plus these scripts are the whole UI gate -- and every rule
// below is one a green `tsc` says nothing about: which step a launch is on is
// a claim about the cluster, and the interesting failures are all claims that
// are merely plausible.
//
//   node src/tabs/cluster/loading.check.mjs
//
// Bundled with esbuild rather than imported directly, because loading.ts has
// a value import of ../../format and node's ESM resolver will not resolve an
// extensionless specifier.

import { build as bundleWithEsbuild } from 'esbuild'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const out = mkdtempSync(join(tmpdir(), 'loading-check-'))
const bundle = join(out, 'loading.mjs')

await bundleWithEsbuild({
  entryPoints: [join(here, 'loading.ts')],
  bundle: true,
  format: 'esm',
  outfile: bundle,
  logLevel: 'warning',
})

const L = await import(pathToFileURL(bundle).href)

let failures = 0
function ok(cond, what) {
  if (!cond) {
    failures++
    console.error(`FAIL  ${what}`)
  } else {
    console.log(`ok    ${what}`)
  }
}

const GiB = 1024 ** 3
const dep = (state, model_id = 'meta-llama/Llama-3.1-8B') => ({ state, model_id })
const launch = (since, last_error = null) => ({
  deployment_id: 'd1',
  served_name: 'llama',
  model_id: 'meta-llama/Llama-3.1-8B',
  runtime: 'vllm',
  state: 'launching',
  node_ids: ['spark-01'],
  since,
  last_error,
})
const pull = (over) => ({
  pull_id: 'p1',
  provider_id: 'ollama',
  provider: 'Ollama',
  model: 'meta-llama/Llama-3.1-8B',
  completed: 0,
  total: null,
  status: 'pulling manifest',
  error: null,
  done: false,
  ...over,
})

const at = (view) => view.steps.findIndex((s) => s.state === 'now')
const labels = (view) => view.steps.map((s) => `${s.state}:${s.phase}`)

// ── Which states get the loading form ───────────────────────────────────────

ok(L.isArriving('planned') && L.isArriving('launching'), 'planned and launching are arriving')
ok(
  !L.isArriving('ready') && !L.isArriving('degraded') && !L.isArriving('stopping') && !L.isArriving('failed'),
  'ready, degraded, stopping and failed are not',
)
// DEGRADED is the one that has to be argued rather than assumed: it is a model
// that is up and serving badly, which the band's own word already says, and
// drawing it as arriving would report a live deployment as one that has not
// got here yet. Same allowlist the gateway's _ARRIVING_STATES uses.
ok(!L.isArriving('degraded'), 'degraded is serving badly, not arriving')

// ── The stepper ─────────────────────────────────────────────────────────────

{
  const v = L.launchView(dep('planned'), null, [], 0)
  ok(at(v) === 0, 'planned is on the first step')
  ok(
    labels(v).join(' ') === 'now:preparing todo:downloading todo:loading todo:serving',
    'and nothing after it is claimed',
  )
}

{
  const v = L.launchView(dep('launching'), null, [], 0)
  ok(at(v) === 1, 'launching with nothing reporting holds at Downloading weights')
  ok(v.steps[1].detail === '', 'and shows no size, because none was reported')
  // The load-bearing one. The control plane cannot see inside the runtime's
  // own fetch, so the two middle steps cannot be told apart from here. Holding
  // at the download is the choice that stays true either way: a launch spends
  // its time there, and "Loading onto the GPU" would assert the weights had
  // landed. Never assert the stronger of two claims you cannot check.
  ok(v.steps[2].state === 'todo', 'and never claims the weights are on the GPU')
}

{
  const v = L.launchView(dep('ready'), null, [], 0)
  ok(v.steps.every((s) => s.state === 'done'), 'ready ticks every step')
  ok(at(v) === -1, 'and leaves none current')
}
ok(
  L.launchView(dep('degraded'), null, [], 0).steps.every((s) => s.state === 'done'),
  'degraded is serving, so its steps are done too',
)

// ── What a real pull moves ──────────────────────────────────────────────────

{
  const p = pull({ completed: 1.5 * GiB, total: 4 * GiB, status: 'pulling' })
  const v = L.launchView(dep('launching'), null, [p], 0)
  ok(at(v) === 1, 'a pull in flight keeps it on Downloading weights')
  ok(v.steps[1].detail === '1.5 / 4.0 GB', 'and fills in the real bytes against the real total')
}
{
  const p = pull({ completed: 4 * GiB, total: 4 * GiB, done: true })
  const v = L.launchView(dep('launching'), null, [p], 0)
  ok(at(v) === 2, 'a finished pull is the one thing that advances it to the GPU load')
}
{
  // Sizing: a pull spends its first seconds with no total at all, and a
  // denominator of zero is an invented measurement. Same rule as activity.ts.
  const v = L.launchView(dep('launching'), null, [pull({ completed: 900e6, total: null })], 0)
  ok(v.steps[1].detail === '', 'a pull that has not been sized shows no fraction')
}
{
  const v = L.launchView(dep('launching'), null, [pull({ model: 'some/other-model' })], 0)
  ok(at(v) === 1 && v.steps[1].detail === '', "another model's pull is not this one's")
}
{
  const v = L.launchView(dep('launching'), null, [pull({ error: 'no space left on device' })], 0)
  ok(at(v) === 1, 'a failed pull is not read as progress')
}
{
  const v = L.launchView({ state: 'launching', model_id: '' }, null, [pull({ model: '' })], 0)
  ok(at(v) === 1 && v.steps[1].detail === '', 'an empty model id matches no pull')
}

// ── The clock ───────────────────────────────────────────────────────────────

ok(L.launchView(dep('launching'), null, [], 1000).elapsed === null, 'no launch row is no clock')
// Null is not zero, and this is the whole reason it is null: `0:00` says the
// launch started this second, which is a stronger claim than "we do not know
// when it started". Same rule the sidebar's activity rows are pinned on.
ok(L.launchView(dep('launching'), launch(940), [], 1000).elapsed === 60, 'elapsed counts from `since`')
ok(
  L.launchView(dep('launching'), launch(1001), [], 1000).elapsed === 0,
  'a coordinator clock ahead of this browser never draws a negative age',
)
ok(L.launchView(dep('launching'), launch(0, 'boom'), [], 0).error === 'boom', 'the last error is carried verbatim')

ok(L.clock(0) === '0:00', 'the clock pads seconds')
ok(L.clock(9) === '0:09' && L.clock(61) === '1:01' && L.clock(600) === '10:00', 'and does not pad minutes')
ok(L.clock(-5) === '0:00', 'and never counts backwards')

rmSync(out, { recursive: true, force: true })
console.log(failures ? `\n${failures} failed` : '\nall good')
process.exit(failures ? 1 : 0)
