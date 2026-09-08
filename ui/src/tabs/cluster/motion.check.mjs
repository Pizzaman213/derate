// Verifier for the floor's motion arithmetic.
//
//   node src/tabs/cluster/motion.check.mjs
//
// motion.ts is bundled with the esbuild inside vite rather than imported, for
// the reason layout.check.mjs bundles: it has a type-only import of layout.ts,
// which has a value import of ../../format, and node's ESM resolver will not
// resolve an extensionless specifier.
//
// What is worth checking here is not that a lerp lerps. It is the two rules
// the motion has to keep, both of which are invisible to the typechecker:
//
//   the ends are EXACT     -- a tween that lands a fraction of a unit off puts
//                             the drawing somewhere layoutCluster did not, and
//                             the next poll snaps it the rest of the way
//   a shape change SNAPS   -- interpolating a bracket into a hop draws a
//                             diagonal halfway through, and a slant on this
//                             floor is the thing the router exists to prevent

import { build as bundleWithEsbuild } from 'esbuild'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const out = mkdtempSync(join(tmpdir(), 'motion-check-'))
const bundle = join(out, 'motion.mjs')

await bundleWithEsbuild({
  entryPoints: [join(here, 'motion.ts')],
  bundle: true,
  format: 'esm',
  platform: 'neutral',
  outfile: bundle,
  logLevel: 'warning',
})

const M = await import(pathToFileURL(bundle).href)

let failures = 0
let checks = 0
function ok(cond, what) {
  checks++
  if (!cond) {
    failures++
    console.error(`  FAIL  ${what}`)
  }
}

const p = (x, y) => ({ x, y })
const same = (a, b) =>
  a.length === b.length && a.every((q, i) => q.x === b[i].x && q.y === b[i].y)

// ── shifts ───────────────────────────────────────────────────────────────────

{
  const prev = new Map([['a', p(0, 0)], ['b', p(50, 0)], ['gone', p(9, 9)]])
  const next = new Map([['a', p(0, 0)], ['b', p(50, 40)], ['new', p(0, 80)]])
  const s = M.shifts(prev, next)

  ok(!s.has('a'), 'a plate that did not move is not transformed at all')
  ok(s.get('b')?.x === 0 && s.get('b')?.y === -40, 'a plate that moved is pushed back to where it was')
  ok(!s.has('new'), 'a machine that has just joined is not slid in from somewhere')
  ok(!s.has('gone'), 'and one that has left has nothing to move')
  ok(s.size === 1, `only the movers are listed (${s.size})`)

  // The sign is the whole point: the drawing is rendered where it ARRIVED and
  // pushed back, so the transform has to be old-minus-new. Getting it the
  // other way round animates every plate away from where it is going.
  const back = M.shifts(new Map([['x', p(10, 10)]]), new Map([['x', p(30, 25)]]))
  ok(back.get('x').x === -20 && back.get('x').y === -15, 'the push is the old position minus the new one')

  ok(M.shifts(new Map(), new Map()).size === 0, 'two empty floors move nothing')
}

// ── sameShape ────────────────────────────────────────────────────────────────

{
  const a = [p(0, 0), p(10, 0), p(10, 20)]
  const b = [p(0, 0), p(30, 0), p(30, 50)]
  ok(M.sameShape(a, b), 'two routes with the same corners interpolate')
  ok(!M.sameShape(a, [p(0, 0), p(10, 0)]), 'a route that lost a corner does not')

  // Same vertex count, but the first run turned through ninety degrees. There
  // is no rectilinear route between these two and the midpoint of the naive
  // interpolation is a diagonal.
  const turned = [p(0, 0), p(0, 10), p(20, 10)]
  ok(!M.sameShape(a, turned), 'a run that changed axis does not')
  ok(!M.sameShape([], []), 'an empty route is not a shape')
  ok(!M.sameShape([p(1, 1)], [p(2, 2)]), 'nor is a single point')
}

// ── lerpRoute ────────────────────────────────────────────────────────────────

{
  const a = [p(0, 0), p(10, 0), p(10, 20)]
  const b = [p(0, 0), p(30, 0), p(30, 50)]

  ok(same(M.lerpRoute(a, b, 0), a), 't=0 is exactly the route it is leaving')
  ok(same(M.lerpRoute(a, b, 1), b), 't=1 is exactly the route it is arriving at')
  ok(same(M.lerpRoute(a, b, -5), a), 'a t below zero is clamped, not extrapolated')
  ok(same(M.lerpRoute(a, b, 9), b), 'and a t above one')

  const half = M.lerpRoute(a, b, 0.5)
  ok(same(half, [p(0, 0), p(20, 0), p(20, 35)]), 'halfway is halfway')

  // The rule the whole tween rests on: an intermediate frame is still a
  // drawing this floor is allowed to make.
  let slant = null
  for (let i = 0; i <= 20; i++) {
    const r = M.lerpRoute(a, b, i / 20)
    for (let k = 1; k < r.length; k++) {
      if (r[k - 1].x !== r[k].x && r[k - 1].y !== r[k].y) slant ??= `t=${i / 20} run ${k}`
    }
  }
  ok(slant == null, `every frame of a tween is still axis-aligned${slant ? ` (${slant})` : ''}`)

  ok(M.lerpRoute(a, [p(0, 0), p(0, 10), p(20, 10)], 0.5) === null, 'a shape change is a snap, not a morph')
  ok(M.lerpRoute(a, [p(0, 0), p(1, 0)], 0.5) === null, 'and so is a corner count change')

  // Reduced motion is not a special case in this file: the caller does not
  // start a tween at all. What it must not do is change where things land,
  // which is what t=1 being exact says.
  ok(same(M.lerpRoute(a, b, 1), b), 'skipping the tween lands in the same place as finishing it')
}

// ── ease ─────────────────────────────────────────────────────────────────────

{
  ok(M.ease(0) === 0 && M.ease(1) === 1, 'the easing pins both ends')
  ok(M.ease(-1) === 0 && M.ease(2) === 1, 'and clamps outside them')

  let backwards = null
  let prev = -1
  for (let i = 0; i <= 100; i++) {
    const v = M.ease(i / 100)
    if (v < prev) backwards ??= `t=${i / 100}`
    prev = v
  }
  ok(backwards == null, `the easing never goes backwards${backwards ? ` (${backwards})` : ''}`)

  // cubic-bezier(0.2, 0.7, 0.3, 1) leaves fast and settles slow, which is what
  // makes the arrival read as arriving rather than as stopping.
  ok(M.ease(0.5) > 0.5, `it is ahead of linear halfway through (${M.ease(0.5).toFixed(3)})`)
  ok(M.ease(0.9) > 0.95, 'and nearly there at nine tenths')
}

console.log('motion.check')
if (failures) console.error(`  ${failures} of ${checks} checks FAILED`)
else console.log(`  ${checks}/${checks} checks passed`)
rmSync(out, { recursive: true, force: true })
process.exit(failures ? 1 : 0)
