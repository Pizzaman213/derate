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
// dashboard's planner bar: `modelGroups` was that bar's model picker. It
// bundles `gates.ts` through the harness, so the logic checked here is the
// logic the screen runs.

import assert from 'node:assert/strict'
import { load } from '../../check/harness.mjs'

let checks = 0
const ok = (label, fn) => {
  fn()
  checks += 1
  console.log(`  ok  ${label}`)
}

// ── The serve-gate derivation ────────────────────────────────────────────────
//
// `gates.ts` is the one copy: `useModelSession` derives the gates with it and
// `VerdictBox` decides whether Serve appears with it.

const G = await load(import.meta.url, './gates.ts')
const gatesOf = G.gatesOf
const canServeWith = (serve, gates, granted, fit = null) =>
  G.canServeWith(serve, fit, gates, granted)

console.log('serve gates')

ok('a gateway that predates `serve` offers no gates', () => {
  assert.deepEqual(gatesOf(undefined), [])
})

ok('with no `serve`, the static fit decides: fits serves, wont_fit does not', () => {
  assert.equal(canServeWith(undefined, [], {}, { verdict: 'fits' }), true)
  assert.equal(canServeWith(undefined, [], {}, { verdict: 'fits_degraded' }), true)
  assert.equal(canServeWith(undefined, [], {}, { verdict: 'wont_fit' }), false)
  // No fit port at all is not a pass either: nothing said it fits.
  assert.equal(canServeWith(undefined, [], {}, null), false)
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
