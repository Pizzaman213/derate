// Verifier for the shape of a wire (issue #5): square-cornered orthogonal
// steps, never a curve, and the same shape while dragging as once dropped.
//
//   node src/tabs/cluster/edges.check.mjs
//
// Types cannot see any of this: `getBezierPath` and `getSmoothStepPath` have
// the same signature and the same return tuple, so swapping one back for the
// other typechecks, renders, and silently brings the curves back.

import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { load, report } from '../../check/harness.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const E = await load(import.meta.url, './edges.tsx')
const S = await load(import.meta.url, './sockets.ts')
const { check, done } = report()

/** `M`/`L` commands -> the points they visit; null on anything else. */
function polyline(d) {
  const tokens = d.match(/[A-Za-z]|-?[\d.]+(?:e-?\d+)?/g) ?? []
  const pts = []
  let cmd = null
  for (let i = 0; i < tokens.length; ) {
    if (/[A-Za-z]/.test(tokens[i])) {
      cmd = tokens[i++]
      if (cmd !== 'M' && cmd !== 'L') return null
      continue
    }
    pts.push({ x: Number(tokens[i]), y: Number(tokens[i + 1]) })
    i += 2
  }
  return pts
}

const near = (a, b) => Math.abs(a - b) < 1e-6

// Every socket pairing the graph draws: requests/responses leave right and
// enter left; machine links leave the bottom and enter the top. Forward,
// backward and level cases, because the backward one is where a step router
// adds the most corners.
const CASES = [
  ['right->left, forward', 0, 0, 'right', 240, 120, 'left'],
  ['right->left, backward', 300, 40, 'right', -80, 200, 'left'],
  ['right->left, level', 10, 50, 'right', 400, 50, 'left'],
  ['bottom->top, below', 100, 0, 'bottom', 180, 260, 'top'],
  ['bottom->top, above', 100, 300, 'bottom', -40, 20, 'top'],
  ['fractional coordinates', 12.5, 33.25, 'right', 410.75, -18.5, 'left'],
]

for (const [what, sx, sy, sp, tx, ty, tp] of CASES) {
  const [d, lx, ly] = E.wirePath({ sourceX: sx, sourceY: sy, sourcePosition: sp, targetX: tx, targetY: ty, targetPosition: tp })
  const pts = polyline(d)
  check(pts !== null, `${what}: only M/L commands (no curve, no rounded corner): ${d}`)
  if (!pts) continue
  const first = pts[0]
  const last = pts[pts.length - 1]
  check(near(first.x, sx) && near(first.y, sy), `${what}: starts on its source socket`)
  check(near(last.x, tx) && near(last.y, ty), `${what}: ends on its target socket`)
  const diagonal = pts.slice(1).some((p, i) => !near(p.x, pts[i].x) && !near(p.y, pts[i].y))
  check(!diagonal, `${what}: every segment is horizontal or vertical`)
  check(Number.isFinite(lx) && Number.isFinite(ly), `${what}: has a label point for the label and the cut button`)
}

{
  const squared = E.squareCorners('M0 0L 10,0Q 10,0 10,0L 10,5')
  check(!squared.includes('Q') && squared.includes('L 10,0') && squared.includes('L 10,5'), `squareCorners drops a zero-size corner and keeps both runs: ${squared}`)
}
check(E.squareCorners('M0 0Q 5,5 10,0').includes('Q'), 'squareCorners leaves a real curve alone (it is not a curve-hider)')

check(typeof S.WIRE_STEP_OFFSET === 'number' && S.WIRE_STEP_OFFSET > 0, 'WIRE_STEP_OFFSET is one positive number')

// The source of every module in the graph: nothing may reach for a curve,
// and the canvas must draw its drag preview with the wire's own shape.
const sources = readdirSync(here)
  .filter((f) => /\.(ts|tsx)$/.test(f))
  .concat(readdirSync(join(here, 'blocks')).filter((f) => /\.(ts|tsx)$/.test(f)).map((f) => join('blocks', f)))
  .map((f) => [f, readFileSync(join(here, f), 'utf8')])
const curvy = sources.filter(([, src]) => /getBezierPath|getSimpleBezierPath|ConnectionLineType\.(Bezier|SimpleBezier|SmoothStep)/.test(src))
check(curvy.length === 0, `no cluster module draws a curve${curvy.length ? ': ' + curvy.map(([f]) => f).join(', ') : ''}`)
const canvas = readFileSync(join(here, 'WiringCanvas.tsx'), 'utf8')
check(/connectionLineComponent=\{WireConnectionLine\}/.test(canvas), 'the drag preview is WireConnectionLine, not React Flow\'s default bezier')
const edges = readFileSync(join(here, 'edges.tsx'), 'utf8')
check((edges.match(/wirePath\(/g) ?? []).length >= 3, 'WireEdge and WireConnectionLine both go through wirePath')

done()
