// Imperative rAF particle layer. Ports mockups-next/js/cluster.js's fly() --
// a small block walked along a path, removed at the end -- into a React ref
// this component never reconciles: a block's position is a DOM attribute write
// every frame, not state, so a flight survives a pan/zoom and costs nothing in
// the render tree.
//
// What is NOT ported is the mockup's fiction. There, flights were emitted on a
// fixed 380ms metronome for as long as anything looked busy, and each one drew
// its path from a client-side weighted coin toss. Neither number came off the
// wire, so the animation was decoration shaped like telemetry.
//
// Here one block means one thing:
//
//     A BLOCK IN FLIGHT ON A PATH IS A REQUEST IN FLIGHT ON THAT TARGET.
//
// The count of blocks moving along a target's path is that target's measured
// outstanding-request count -- `stats.begin()` on dispatch, `complete()`/
// `fail()` on settle, read back as the deployment frame's `queue_depth` at 1 Hz
// or as `RouteTarget.outstanding` on the routing poll. Idle targets emit
// nothing, a saturated one carries as many blocks as it has requests, and
// nothing is drawn on a path no request is on. The flight's duration is that
// target's own EWMA of real request durations.
//
// The honest limits, since they are limits and not fudges:
//   * Sampling. A request that begins AND ends between two samples was never
//     visible to us and gets no block. Everything longer than the 1 Hz frame
//     interval is seen.
//   * Speed clamps. `mean_duration_s` outside [MIN_FLIGHT_MS, MAX_FLIGHT_MS]
//     is clamped, because a 40ms flight is a flicker and a 90s one reads as a
//     stall. Past the clamp the block's speed stops tracking the measurement;
//     its presence still does.

import { useEffect, useRef, type RefObject } from 'react'
import type { Point } from './layout'

/** Used only when a target has requests in flight but has never completed one,
 *  so there is no measured duration to fly at yet. */
export const FLIGHT_MS = 1600
export const MIN_FLIGHT_MS = 700
export const MAX_FLIGHT_MS = 8000
/** How often the layer tops paths up to their measured in-flight count. Also
 *  the stagger: at most one new flight per path per tick, so four concurrent
 *  requests read as four blocks spread down the line rather than one thick
 *  one. Well under the 1 Hz frame that feeds it. */
export const TOPUP_MS = 200

/** One routing target's live contribution to the animation. */
export interface TargetFlow {
  /** The path this target's requests walk, from `localFlightKey` /
   *  `providerFlightKey`. Targets that share a key (every remote target of a
   *  served name shares the provider rail) have their counts summed. */
  pathKey: string
  /** Requests measured in flight on this target right now. */
  inflight: number
  /** This target's EWMA of real request durations, seconds. Null until it has
   *  completed one. */
  meanDurationS: number | null
}

/** How long one block takes to walk the path: the target's own measured mean
 *  request duration, clamped to what the eye can follow. */
export function flightMs(meanDurationS: number | null): number {
  if (meanDurationS == null || !Number.isFinite(meanDurationS) || meanDurationS <= 0) return FLIGHT_MS
  return Math.min(MAX_FLIGHT_MS, Math.max(MIN_FLIGHT_MS, meanDurationS * 1000))
}

/** Sums flows onto the paths that were actually drawn. Flows naming a path
 *  this layout has no geometry for are dropped rather than redirected -- a
 *  request on an undrawn target is better shown nowhere than shown on the
 *  wrong machine. The duration kept for a shared path is the longest of its
 *  contributors', so a slow provider is not drawn at a local target's pace. */
export function collapseFlows(flows: TargetFlow[], paths: Record<string, Point[]>): Map<string, TargetFlow> {
  const byPath = new Map<string, TargetFlow>()
  for (const f of flows) {
    if (!paths[f.pathKey]) continue
    const n = Math.max(0, Math.floor(f.inflight))
    if (n === 0) continue
    const prev = byPath.get(f.pathKey)
    if (!prev) {
      byPath.set(f.pathKey, { ...f, inflight: n })
      continue
    }
    prev.inflight += n
    if ((f.meanDurationS ?? 0) > (prev.meanDurationS ?? 0)) prev.meanDurationS = f.meanDurationS
  }
  return byPath
}

/** One flight: a small rect walked along `pts` over `durationMs`, then removed.
 *  `layer` is never touched by React -- this is the only thing that mutates
 *  it, and it does so with plain DOM calls so the flight keeps running
 *  through any number of parent re-renders. `onDone` fires exactly once, when
 *  the block leaves, so the caller's in-flight tally can drop. */
export function spawnParticle(
  layer: SVGGElement,
  pts: Point[],
  color: string,
  durationMs: number = FLIGHT_MS,
  onDone?: () => void,
): void {
  if (pts.length < 2) {
    onDone?.()
    return
  }
  const NS = 'http://www.w3.org/2000/svg'
  const box = document.createElementNS(NS, 'rect')
  box.setAttribute('width', '11')
  box.setAttribute('height', '9')
  box.setAttribute('rx', '1.5')
  box.setAttribute('fill', color)
  layer.appendChild(box)

  const segs: number[] = []
  let total = 0
  for (let i = 1; i < pts.length; i++) {
    const L = Math.hypot(pts[i]!.x - pts[i - 1]!.x, pts[i]!.y - pts[i - 1]!.y)
    segs.push(L)
    total += L
  }

  const t0 = performance.now()
  const span = Math.max(1, durationMs)

  const step = (now: number) => {
    const p = Math.min((now - t0) / span, 1)
    let d = p * total
    let i = 0
    while (i < segs.length && d > segs[i]!) {
      d -= segs[i]!
      i++
    }
    if (i >= segs.length) i = segs.length - 1
    const a = pts[i]!
    const b = pts[i + 1] ?? pts[i]!
    const f = segs[i] ? d / segs[i]! : 0
    box.setAttribute('x', String(a.x + (b.x - a.x) * f - 5))
    box.setAttribute('y', String(a.y + (b.y - a.y) * f - 4))
    // A parent unmount can detach `layer` before this flight finishes; the
    // rect just keeps walking off-tree for the rest of its span and is
    // garbage the moment the callback stops, so there is nothing to guard.
    if (p < 1) requestAnimationFrame(step)
    else {
      box.remove()
      onDone?.()
    }
  }
  requestAnimationFrame(step)
}

export interface ParticleFieldOptions {
  layerRef: RefObject<SVGGElement>
  /** Live per-target request counts for the selected deployment. Empty means
   *  nothing selected, nothing routed, or nothing in flight -- all three draw
   *  the same thing, which is nothing. */
  flows: TargetFlow[]
  paths: Record<string, Point[]>
}

/** Mount-scoped top-up loop, cancelled on unmount and fully suppressed under
 *  prefers-reduced-motion.
 *
 *  Every tick, each path whose measured in-flight count exceeds the number of
 *  blocks currently walking it launches one more. Blocks retire on their own
 *  when their flight ends; a request that outlives its block is represented by
 *  the next launch, so the population on a path tracks `inflight` continuously
 *  rather than only at request boundaries. Nothing here re-runs on re-render:
 *  `flows` and `paths` are read through a ref, so panning the graph or a
 *  routing poll landing never restarts the loop or resets a tally. */
export function useParticleField(opts: ParticleFieldOptions): void {
  const { layerRef } = opts
  const latest = useRef(opts)
  latest.current = opts

  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return

    // pathKey -> blocks currently walking it. Owned by this effect, so an
    // unmount drops it wholesale and a remount starts from zero rather than
    // inheriting a count for flights it can no longer see.
    const active = new Map<string, number>()

    const id = window.setInterval(() => {
      const layer = latest.current.layerRef.current
      if (!layer) return
      const byPath = collapseFlows(latest.current.flows, latest.current.paths)
      for (const [key, flow] of byPath) {
        const running = active.get(key) ?? 0
        if (running >= flow.inflight) continue
        const pts = latest.current.paths[key]
        if (!pts) continue
        active.set(key, running + 1)
        spawnParticle(layer, pts, 'var(--flow)', flightMs(flow.meanDurationS), () => {
          active.set(key, Math.max(0, (active.get(key) ?? 1) - 1))
        })
      }
    }, TOPUP_MS)

    return () => window.clearInterval(id)
  }, [layerRef])
}
