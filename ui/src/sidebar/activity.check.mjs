// Verifier for activity.ts, the rail's "what is happening" rows. Same rule as
// ollamaTarget.check.mjs and rows.check.mjs: there is no UI test runner here,
// so esbuild-bundle the module and exercise it, because a green tsc says
// nothing about whether a bar draws an honest number.
//
// The rules below are all ones types cannot catch. Two of them -- null is not
// zero, and a launch has no percentage -- are the entire reason this file
// exists: both compile perfectly while putting an invented figure on screen.
//
//   node ui/src/sidebar/activity.check.mjs
import { build as bundleWithEsbuild } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const out = mkdtempSync(join(tmpdir(), 'activity-'))
const bundle = join(out, 'bundle.mjs')
// esbuild's JS API rather than spawning `npx`: on Windows that resolves to
// npx.cmd, which cannot be launched by path without a shell.
await bundleWithEsbuild({
  entryPoints: [join(here, 'activity.ts')],
  bundle: true,
  format: 'esm',
  outfile: bundle,
  logLevel: 'warning',
})
const { activityRows, downloadValue, launchValue } = await import(pathToFileURL(bundle).href)

let failures = 0
const check = (name, actual, expected) => {
  const ok = Object.is(actual, expected)
  if (!ok) failures++
  console.log(
    `${ok ? 'ok  ' : 'FAIL'} ${name}` +
      (ok ? '' : `\n       got ${JSON.stringify(actual)}\n       want ${JSON.stringify(expected)}`),
  )
}

const GIB = 1024 ** 3

const dl = (over = {}) => ({
  pull_id: 'pull-1',
  provider_id: 'ollama-1',
  provider: 'Ollama on connor-pi',
  model: 'qwen2.5:0.5b',
  completed: 0,
  total: null,
  status: '',
  error: null,
  done: false,
  ...over,
})

const launch = (over = {}) => ({
  deployment_id: 'd-1',
  served_name: 'gpt-oss-120b',
  model_id: 'openai/gpt-oss-120b',
  runtime: 'vllm',
  state: 'launching',
  node_ids: ['spark-01', 'spark-02'],
  since: 1000,
  last_error: null,
  phase: null,
  status: '',
  fraction: null,
  eta_s: null,
  fatal: false,
  ...over,
})

console.log('--- a download with no total yet is unmeasured, not empty ---')

// The load-bearing one. `ProportionBar` draws null as a dashed empty track and
// 0 as a solid empty one, deliberately, so that a missing value is never
// pixel-identical to zero. A pull spends its first seconds in "pulling
// manifest" with no total; returning 0 there would state that a download
// nothing has measured is 0% done.
check('no total => null, never 0', downloadValue(dl({ total: null })), null)
check('total 0 => null', downloadValue(dl({ total: 0 })), null)
check('a real 0% stays 0', downloadValue(dl({ total: 100, completed: 0 })), 0)
check('half is half', downloadValue(dl({ total: 100, completed: 50 })), 0.5)

// A provider's last frame can report a `completed` a few bytes past its own
// `total`. A bar wider than its track is a rendering bug on somebody else's
// arithmetic.
check('overshoot clamps to 1', downloadValue(dl({ total: 100, completed: 104 })), 1)
check('negative clamps to 0', downloadValue(dl({ total: 100, completed: -4 })), 0)

console.log('\n--- a launching model has no percentage unless the runtime counted one ---')

// A launch is four slow steps and exactly one of them is measured: the
// runtime counts its own checkpoint shards. Everything else -- the image
// pull, the weight download, the compile, the graph capture -- reports no
// denominator, so the row carries no number for them. A figure appearing
// here for any other phase means something upstream started guessing.
const [only] = activityRows({ downloads: [], launches: [launch()] })
check('no fraction reported => null, never 0', only.value, null)
check('launch kind', only.kind, 'launch')
check('launch title is the served name', only.title, 'gpt-oss-120b')
check('launch target is its nodes', only.target, 'spark-01, spark-02')
check('launch status falls back to the state', only.status, 'launching')

check('a measured fraction is kept', launchValue(launch({ fraction: 0.45 })), 0.45)
check('a real 0 stays 0', launchValue(launch({ fraction: 0 })), 0)
check('null is not 0', launchValue(launch({ fraction: null })), null)
check('overshoot clamps to 1', launchValue(launch({ fraction: 1.4 })), 1)
check('negative clamps to 0', launchValue(launch({ fraction: -0.2 })), 0)
// NaN reaching a bar's width is a blank track with no explanation.
check('NaN is unmeasured, not a width', launchValue(launch({ fraction: NaN })), null)

console.log('\n--- the launcher and the runtime speak for themselves ---')

// Verbatim, like the download rows: "Loading safetensors checkpoint shards:
// 45% Completed | 5/11" is more precise than any sentence written in the UI,
// and re-casing or trimming it is what the Verbatim rule exists to stop.
const loading = activityRows({
  downloads: [],
  launches: [
    launch({
      phase: 'loading',
      status: 'Loading safetensors checkpoint shards:  45% Completed | 5/11',
      fraction: 5 / 11,
    }),
  ],
})[0]
check('the runtime sentence is passed through', loading.status, 'Loading safetensors checkpoint shards:  45% Completed | 5/11')
check('the shard count draws the bar', loading.value, 5 / 11)
check('the step leads the detail line', loading.detail, 'Loading onto the GPU · vllm')

// A phase this build has not heard of contributes no caption rather than a
// raw identifier, and never blanks the runtime beside it.
check(
  'an unknown phase degrades to the runtime alone',
  activityRows({ downloads: [], launches: [launch({ phase: 'quantising' })] })[0].detail,
  'vllm',
)
// The downloader and the checkpoint loader both print their own remaining
// time; the image pull, the compile and the graph capture report no total at
// all. A step that measures nothing contributes no estimate rather than a
// guess, and the estimate is rounded because tqdm's is extrapolated.
check(
  'a reported estimate rides beside the step',
  activityRows({
    downloads: [],
    launches: [launch({ phase: 'downloading', eta_s: 195 })],
  })[0].detail,
  'Downloading weights · about 3 min left · vllm',
)
check(
  'no estimate contributes nothing',
  activityRows({ downloads: [], launches: [launch({ phase: 'downloading' })] })[0].detail,
  'Downloading weights · vllm',
)
check(
  'under a minute is not a countdown',
  activityRows({
    downloads: [],
    launches: [launch({ phase: 'loading', eta_s: 14 })],
  })[0].detail,
  'Loading onto the GPU · under a minute left · vllm',
)

check(
  'no phase yet reads the same way',
  activityRows({ downloads: [], launches: [launch()] })[0].detail,
  'vllm',
)

// The runtime announcing its own death reaches this row before the manager
// has finished writing the failure onto the record. An amber lamp over
// "EngineCore failed to start." is the row contradicting its own caption.
check(
  'a runtime that says it died is a fault, before last_error exists',
  activityRows({
    downloads: [],
    launches: [launch({ fatal: true, status: 'EngineCore failed to start.' })],
  })[0].signal,
  'fault',
)
check('a healthy launch is warn, not fault', only.signal, 'warn')
check('a launch that errored is fault', activityRows({ downloads: [], launches: [launch({ last_error: 'boom' })] })[0].signal, 'fault')

console.log('\n--- the figures under the bar ---')

check(
  'sized download reads as a fraction of itself',
  activityRows({ downloads: [dl({ total: 4 * GIB, completed: GIB })], launches: [] })[0].detail,
  '1.0 / 4.0 GiB',
)
// Not "0.0 / 0.0 GiB": a denominator nothing reported is an invented
// measurement, and this project does not print those.
check(
  'unsized download says so instead of showing a zero denominator',
  activityRows({ downloads: [dl()], launches: [] })[0].detail,
  'sizing',
)

console.log('\n--- lamps follow state, not category ---')
check('running download is warn', activityRows({ downloads: [dl({ total: 10, completed: 5 })], launches: [] })[0].signal, 'warn')
check('finished download is live', activityRows({ downloads: [dl({ done: true })], launches: [] })[0].signal, 'live')
check('failed download is fault', activityRows({ downloads: [dl({ done: true, error: 'connection reset' })], launches: [] })[0].signal, 'fault')
check(
  'a failed download carries the server sentence unchanged',
  activityRows({ downloads: [dl({ error: 'connection reset' })], launches: [] })[0].error,
  'connection reset',
)

console.log('\n--- order is stable while the numbers move ---')

// Nothing sorts by progress. A row that reshuffles as its own bar advances is
// unreadable, and the bar advancing is the one thing guaranteed to happen.
const mixed = activityRows({
  downloads: [
    dl({ pull_id: 'pull-2', model: 'b', total: 100, completed: 99 }),
    dl({ pull_id: 'pull-1', model: 'a', total: 100, completed: 1 }),
  ],
  launches: [launch({ deployment_id: 'd-2', since: 2000 }), launch({ deployment_id: 'd-1', since: 1000 })],
})
check('downloads come before launches', mixed.map((r) => r.kind).join(','), 'download,download,launch,launch')
check('downloads keep the order the server sent', mixed.slice(0, 2).map((r) => r.title).join(','), 'b,a')
check('launches are oldest first', mixed.slice(2).map((r) => r.key).join(','), 'launch:d-1,launch:d-2')

console.log('\n--- keys are unique across kinds ---')
// A deployment id and a pull id are minted by different things and could
// collide; React would then drop a row silently.
check('keys are distinct', new Set(mixed.map((r) => r.key)).size, 4)

console.log('\n--- absence is empty, never a crash ---')
check('null activity', activityRows(null).length, 0)
check('undefined activity', activityRows(undefined).length, 0)
check('missing arrays', activityRows({}).length, 0)

console.log(failures === 0 ? '\nall ok' : `\n${failures} FAILED`)
process.exit(failures === 0 ? 0 : 1)
