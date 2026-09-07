// Verifier for the Serve panel's pure logic. There is no test runner in this
// repo (AUDIT-2026-09-06.md:197: "No UI test suite exists (typecheck is the
// only gate)"), so the part of the serve decision that CAN be checked without
// a browser is checked without one:
//
//   node src/tabs/models/serve.check.mjs
//
// What is covered: the derivation of the launch permissions from `serve`, and
// whether the Serve button is allowed to appear. Both are pure functions of
// wire data, and both have branches a live cluster cannot currently reach -- a
// gateway that predates `serve.overrides`, two gates at once, a gate that
// passes while the fit already allowed the launch.
//
// This was tabs/dashboard/planner.check.mjs. It lost its other half with the
// dashboard's planner bar: `modelGroups` was that bar's model picker, and the
// Serve panel has no picker -- it is already on one model's URL. Nothing is
// bundled any more, so esbuild is no longer involved.

import assert from 'node:assert/strict'

let checks = 0
const ok = (label, fn) => {
  fn()
  checks += 1
  console.log(`  ok  ${label}`)
}

// ── The serve-gate derivation ────────────────────────────────────────────────
//
// Mirrors ServePanel's `gates` memo and Verdict's `canServe`. Kept here rather
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

console.log(`\n${checks} checks passed`)
