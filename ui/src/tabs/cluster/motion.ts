// What the floor looks like PART WAY between two layouts.
//
// No DOM, no React, for the same reason layout.ts has none: this is arithmetic
// on two drawings, and ClusterGraph writes the result onto elements the way
// particles.ts does. That also makes it checkable with a plain node script.
//
// The destination is not this file's business and never becomes it.
// `layoutCluster` decides where every plate and every wire ends up, and it is
// still a pure function of the node set plus the arrangement -- H-ui.md:154's
// "a machine should be in the same place every time somebody looks" is a claim
// about where the motion STOPS, and nothing here moves that. What was missing
// was the journey: the floor re-laid out on a 5s poll and on every selection,
// and a plate that grew in place shifted every row below it by teleporting
// them.
//
// Two rules hold the whole thing up.
//
// A plate is moved by TRANSFORM, never by re-issuing its coordinates. Every
// plate is drawn as a dozen absolutely-placed primitives (`x={card.x + 11}`
// and so on) and there is no useful way to transition a dozen sibling `x`
// attributes; there is a very ordinary way to transition one `transform`. So a
// plate is rendered where it has ARRIVED and then pushed back to where it
// started, and the push is what is animated away. Nothing in the drawing has
// to know.
//
// A wire whose SHAPE changed is snapped, never morphed. Two routes only
// interpolate into a third rectilinear route while they have the same number
// of corners and each run points the same way in both; a pair that flipped
// between a bracket, a hop and a drop does not, and interpolating it anyway
// draws a wire at a slant through whatever is between them. `sameShape` is the
// question, and a `null` from `lerpRoute` means "put the new one up now".

import type { Point } from './layout'

/** Where each identity has to be pushed back to for the drawing to start from
 *  the layout it is leaving: the OLD position minus the new one, applied on
 *  top of the new one.
 *
 *  Only identities in both layouts. Something that has just arrived is not
 *  moving -- it has no previous position to come from, and sliding it in from
 *  wherever the last plate happened to sit would state a relation between two
 *  machines that has nothing to do with each other. Something that left is
 *  gone; there is no element to move. */
export function shifts(
  prev: ReadonlyMap<string, Point>,
  next: ReadonlyMap<string, Point>,
): Map<string, Point> {
  const out = new Map<string, Point>()
  for (const [id, to] of next) {
    const from = prev.get(id)
    if (!from) continue
    const x = from.x - to.x
    const y = from.y - to.y
    // A plate that did not move gets no transform at all rather than a
    // transform of zero: the poll runs every five seconds and all but a
    // handful of those redraw the floor exactly where it already was.
    if (x === 0 && y === 0) continue
    out.set(id, { x, y })
  }
  return out
}

/** Can these two routes be interpolated without leaving the grid?
 *
 *  Same number of corners, and every run pointing the same way in both. A run
 *  that is horizontal at one end of the tween and vertical at the other has no
 *  rectilinear path between them: the midpoint of that interpolation is a
 *  diagonal, which is the one thing the router exists to keep off the floor. */
export function sameShape(from: readonly Point[], to: readonly Point[]): boolean {
  if (from.length !== to.length || from.length < 2) return false
  for (let i = 1; i < from.length; i++) {
    const a0 = from[i - 1]!, a1 = from[i]!
    const b0 = to[i - 1]!, b1 = to[i]!
    const horiz = a0.y === a1.y && b0.y === b1.y
    const vert = a0.x === a1.x && b0.x === b1.x
    if (!horiz && !vert) return false
  }
  return true
}

/** The route `t` of the way from one layout to the other, or `null` when the
 *  two do not have a route between them and the new one should simply be put
 *  up. Both ends are exact: `t` of 0 is the old route and `t` of 1 is the new
 *  one, vertex for vertex, so a tween cannot leave a wire a fraction of a unit
 *  off the geometry the layout decided. */
export function lerpRoute(
  from: readonly Point[],
  to: readonly Point[],
  t: number,
): Point[] | null {
  if (!sameShape(from, to)) return null
  if (t <= 0) return from.map((p) => ({ x: p.x, y: p.y }))
  if (t >= 1) return to.map((p) => ({ x: p.x, y: p.y }))
  return to.map((p, i) => {
    const a = from[i]!
    return { x: a.x + (p.x - a.x) * t, y: a.y + (p.y - a.y) * t }
  })
}

/** The easing the plates get from CSS (`--ease`), so a wire tweened here in
 *  JavaScript arrives with them instead of racing them. cubic-bezier(0.2, 0.7,
 *  0.3, 1) solved for y at a given x, by bisection -- exact enough at 60fps
 *  and a great deal shorter than the closed form. */
export function ease(t: number): number {
  if (t <= 0) return 0
  if (t >= 1) return 1
  const bez = (a: number, b: number, u: number) => {
    const v = 1 - u
    return 3 * v * v * u * a + 3 * v * u * u * b + u * u * u
  }
  let lo = 0
  let hi = 1
  for (let i = 0; i < 20; i++) {
    const mid = (lo + hi) / 2
    if (bez(0.2, 0.3, mid) < t) lo = mid
    else hi = mid
  }
  return bez(0.7, 1, (lo + hi) / 2)
}
