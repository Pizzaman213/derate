// Verifier for the planner bar's pure logic. There is no test runner in this
// repo (AUDIT-2026-09-06.md:197: "No UI test suite exists (typecheck is the
// only gate)"), so the parts of manual placement that CAN be checked without a
// browser are checked without one:
//
//   node src/tabs/dashboard/planner.check.mjs
//
// What is covered: the model picker's grouping and dedupe, and the serve-gate
// derivation. Both are pure functions of wire data, and both have branches that
// a live cluster cannot currently reach -- an ambiguous cache folder, a gateway
// that predates `serve.overrides`, two gates at once. Bundled with the esbuild
// that ships inside vite, for the same reason layout.check.mjs is.

import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..', '..')
const out = mkdtempSync(join(tmpdir(), 'planner-check-'))
const bundle = join(out, 'modelOptions.mjs')

execFileSync(
  join(uiRoot, 'node_modules', '.bin', 'esbuild'),
  [
    join(here, 'modelOptions.ts'),
    '--bundle',
    '--format=esm',
    `--outfile=${bundle}`,
    '--log-level=warning',
  ],
  { stdio: 'inherit' },
)

const { modelGroups } = await import(bundle)

let checks = 0
const ok = (label, fn) => {
  fn()
  checks += 1
  console.log(`  ok  ${label}`)
}

// ── The serve-gate derivation ────────────────────────────────────────────────
//
// Mirrors PlannerBar's `gates` memo and Verdict's `canServe`. Kept here rather
// than imported because both live inside components that pull in React; the
// logic is four lines and the branches are what matter.

const gatesOf = (serve) =>
  serve?.overrides ??
  (serve?.override_required
    ? [{ param: serve.override_param ?? 'allow_over_live_memory', reason: serve.reason }]
    : [])

const canServeWith = (serve, gates, granted) => {
  const fitPassed = serve == null ? true : serve.allowed === true
  const refusedOutright = !fitPassed && gates.length === 0
  return !refusedOutright && gates.every((g) => granted[g.param] === true)
}

console.log('serve gates')

ok('a gateway that predates `serve` offers no gates', () => {
  assert.deepEqual(gatesOf(undefined), [])
})

ok('a passing fit with no overrides serves immediately (today’s behaviour)', () => {
  const serve = { allowed: true, override_required: false, override_param: null, reason: 'fits' }
  const gates = gatesOf(serve)
  assert.deepEqual(gates, [])
  assert.equal(canServeWith(serve, gates, {}), true)
})

ok('the legacy single gate is reconstructed with its param', () => {
  const serve = {
    allowed: false,
    override_required: true,
    override_param: 'allow_over_live_memory',
    reason: "Won't fit right now",
  }
  const gates = gatesOf(serve)
  assert.deepEqual(gates, [
    { param: 'allow_over_live_memory', reason: "Won't fit right now" },
  ])
  assert.equal(canServeWith(serve, gates, {}), false)
  assert.equal(canServeWith(serve, gates, { allow_over_live_memory: true }), true)
})

ok('`overrides` supersedes the legacy pair when both are sent', () => {
  const serve = {
    allowed: false,
    override_required: true,
    override_param: 'allow_over_live_memory',
    reason: 'memory',
    overrides: [
      { param: 'allow_over_live_memory', reason: 'memory' },
      { param: 'allow_mixed_hardware', reason: 'not alike' },
    ],
  }
  const gates = gatesOf(serve)
  assert.equal(gates.length, 2)
  // One button, and it stays away until BOTH are ticked.
  assert.equal(canServeWith(serve, gates, { allow_over_live_memory: true }), false)
  assert.equal(canServeWith(serve, gates, { allow_mixed_hardware: true }), false)
  assert.equal(
    canServeWith(serve, gates, { allow_over_live_memory: true, allow_mixed_hardware: true }),
    true,
  )
})

ok('a gate can be needed while the fit itself passes', () => {
  // The case `allowed: false` could never express, and the reason `overrides`
  // exists: pooling unlike hardware with plenty of memory.
  const serve = {
    allowed: true,
    override_required: false,
    override_param: null,
    reason: 'fits',
    overrides: [{ param: 'allow_mixed_hardware', reason: 'not alike' }],
  }
  const gates = gatesOf(serve)
  assert.equal(canServeWith(serve, gates, {}), false)
  assert.equal(canServeWith(serve, gates, { allow_mixed_hardware: true }), true)
})

ok('a hard refusal offers no way through', () => {
  const serve = {
    allowed: false,
    override_required: false,
    override_param: null,
    reason: 'does not fit this hardware at all',
    overrides: [],
  }
  assert.equal(canServeWith(serve, gatesOf(serve), {}), false)
})

// ── The model picker ─────────────────────────────────────────────────────────

const curated = [
  {
    model_id: 'Qwen/Qwen3-30B-A3B',
    label: 'qwen3-30b-a3b',
    detail: 'MoE',
    default_context: 32768,
    default_concurrency: 8,
  },
]
const deployment = (over = {}) => ({
  deployment_id: 'd1',
  served_name: 'llama',
  model_id: 'meta-llama/Llama-3.3-70B-Instruct',
  runtime: 'vllm',
  state: 'ready',
  context_length: 4096,
  max_concurrent_seqs: 2,
  started_at: null,
  last_error: null,
  node_ids: [],
  plan: {},
  fit: {},
  ...over,
})
const cache = (node_id, repos) => ({
  node_id,
  filesystems: [],
  estate: [],
  unreadable: [],
  available: true,
  reason: null,
  models: { available: true, path: '/x', repos, total_bytes: 1, reason: null },
})
const repo = (repo_id, folder) => ({
  repo_id,
  folder: folder ?? `models--${repo_id.replace(/\//g, '--')}`,
  bytes: 1,
  blob_count: 1,
  revisions: [],
  last_modified: null,
})

const labels = (gs) => gs.map((g) => g.label)
const ids = (gs, label) =>
  (gs.find((g) => g.label === label)?.options ?? []).map((o) => o.model_id)

console.log('model picker')

ok('an empty group renders no optgroup at all', () => {
  const gs = modelGroups({ curated, deployments: [], storage: null, selected: null })
  assert.deepEqual(labels(gs), ['Curated'])
})

ok('a serving model carries its real configured numbers', () => {
  const gs = modelGroups({
    curated,
    deployments: [deployment()],
    storage: null,
    selected: null,
  })
  const opt = gs.find((g) => g.label === 'Serving now').options[0]
  assert.equal(opt.model_id, 'meta-llama/Llama-3.3-70B-Instruct')
  assert.equal(opt.default_context, 4096)
  assert.equal(opt.default_concurrency, 2)
})

ok('a stopped deployment is not offered', () => {
  const gs = modelGroups({
    curated,
    deployments: [deployment({ state: 'stopped' })],
    storage: null,
    selected: null,
  })
  assert.equal(labels(gs).includes('Serving now'), false)
})

ok('curated beats serving beats downloaded, exactly once each', () => {
  const gs = modelGroups({
    curated,
    deployments: [deployment({ model_id: 'Qwen/Qwen3-30B-A3B' })],
    storage: [cache('spark-01', [repo('Qwen/Qwen3-30B-A3B')])],
    selected: null,
  })
  assert.deepEqual(ids(gs, 'Curated'), ['Qwen/Qwen3-30B-A3B'])
  assert.equal(labels(gs).includes('Serving now'), false)
  assert.equal(labels(gs).includes('Downloaded here'), false)
})

ok('the same repository on three machines is one option', () => {
  const gs = modelGroups({
    curated: [],
    deployments: [],
    storage: ['a', 'b', 'c'].map((n) => cache(n, [repo('org/m')])),
    selected: null,
  })
  const opts = gs.find((g) => g.label === 'Downloaded here').options
  assert.equal(opts.length, 1)
  assert.equal(opts[0].detail, 'on 3 machines')
})

ok('a folder whose id cannot be reconstructed is listed but not choosable', () => {
  // `repo_from_folder` is ambiguous by construction: models--a--b--c could be
  // a/b--c or a--b/c. Offering a guess to the planner would be deciding on it.
  const gs = modelGroups({
    curated: [],
    deployments: [],
    storage: [cache('spark-01', [repo('a/b--c', 'models--a--b--c')])],
    selected: null,
  })
  const opt = gs.find((g) => g.label === 'Downloaded here').options[0]
  assert.equal(opt.disabled, true)
  assert.equal(opt.detail, 'ambiguous folder name')
})

ok('case is never folded — HuggingFace ids are case-sensitive', () => {
  const gs = modelGroups({
    curated: [],
    deployments: [],
    storage: [cache('spark-01', [repo('org/Model'), repo('org/model')])],
    selected: null,
  })
  assert.equal(gs.find((g) => g.label === 'Downloaded here').options.length, 2)
})

ok('a selected id present nowhere else still has an option', () => {
  // Otherwise the select renders blank when a serving model stops while chosen.
  const gs = modelGroups({
    curated,
    deployments: [],
    storage: null,
    selected: 'org/vanished',
  })
  assert.deepEqual(ids(gs, 'Selected'), ['org/vanished'])
})

ok('a selected id that IS present gets no duplicate group', () => {
  const gs = modelGroups({
    curated,
    deployments: [],
    storage: null,
    selected: 'Qwen/Qwen3-30B-A3B',
  })
  assert.equal(labels(gs).includes('Selected'), false)
})

rmSync(out, { recursive: true, force: true })
console.log(`\n${checks} checks passed`)
