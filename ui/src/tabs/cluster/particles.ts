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
// and on the return leg, a second claim in a deliberately different shape:
//
//     A BLOCK LEAVING A BAND IS OUTPUT TOKENS LEAVING THAT SERVED NAME.
//
// Inbound, the POPULATION is the measurement: N blocks is N requests, and the
// layer counts them up and down. Outbound, the RATE is the measurement and the
// population means nothing -- blocks leave at a compressed, CAPPED image of
// measured tokens/sec, so nobody can count blocks and arrive at a token
// figure. The token figure is on the band, in tok/s, one number, where it
// always was. Outbound flights therefore fly at a CONSTANT speed: spacing
// carries the rate, and encoding one number in two channels means neither of
// them is readable.
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
import type { MetricsDeploymentFrame, RoutingConfig } from '../../api/types'
import { localFlightKey, providerFlightKey, type Point } from './layout'

/** The block's width across the wire. Deliberately inside the wire's own
 *  range (`edgeWidth` in layout.ts tops out at 5.7 for a saturated link) so
 *  the block reads as a lit-up piece of the wire, the way the header logo's
 *  pulse does, rather than a shape sitting on top of a thinner line. */
export const PARTICLE_STROKE_WIDTH = 3

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
   *  served name shares the one drop into the provider box) have their counts
   *  summed. */
  pathKey: string
  /** Requests measured in flight on this target right now. */
  inflight: number
  /** This target's EWMA of real request durations, seconds. Null until it has
   *  completed one. */
  meanDurationS: number | null
}

/** Every routing target's live contribution, for EVERY served name.
 *
 *  It is not scoped to the selection, and that is the point. This used to open
 *  with `if (!selDep) return []`, so a graph with nothing clicked -- the state
 *  it is in almost all the time -- drew no blocks at all, and a cluster whose
 *  only traffic went to a provider never animated once. Nothing about the
 *  claim weakens by drawing all of it: a block is still one measured request
 *  on one measured target, and `collapseFlows` still drops any flow whose path
 *  this layout did not draw, so the names that are not on the floor cost a map
 *  and contribute nothing.
 *
 *  Pure, and exported for that reason: the rule about which counter is read at
 *  which cadence is the kind of thing a typechecker cannot hold. */
export function targetFlows(
  routing: RoutingConfig[],
  frames: MetricsDeploymentFrame[] | null | undefined,
  stale: boolean,
): TargetFlow[] {
  return routing.flatMap((cfg) =>
    cfg.targets.map((t) => {
      // Two reads of the same counter at two cadences. The 1 Hz stream carries
      // it for local targets as `queue_depth`; the 5s routing poll carries it
      // for every target as `outstanding`. Prefer the fresher one where it
      // exists, and drop back to the slower one the moment the stream goes
      // stale rather than freezing on a count that has stopped arriving.
      const live =
        t.kind === 'local' && !stale
          ? (frames ?? []).find((d) => d.deployment_id === t.target_id)?.queue_depth
          : null
      return {
        pathKey:
          t.kind === 'remote'
            ? providerFlightKey(cfg.served_name)
            : localFlightKey(cfg.served_name, t.target_id),
        inflight: Math.max(0, live ?? t.outstanding ?? 0),
        meanDurationS: t.counters?.mean_duration_s ?? null,
      }
    }),
  )
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

// ── The return leg: a rate, not a population ────────────────────────────────

/** Out-blocks fly at a constant speed. See the header: the rate is carried by
 *  how far apart they are, so putting it in the speed too would encode one
 *  number in two channels and make neither readable. */
export const STREAM_FLIGHT_MS = 900
/** Below this there is no rate worth drawing. Not zero -- a trickle under one
 *  token a second is not a stream, it is a rounding artefact. */
export const STREAM_MIN_TPS = 1
export const STREAM_MIN_HZ = 0.5
/** The hard ceiling, and it is the top-up tick's own: at most one block per
 *  path per tick. A 3000 tok/s deployment emits five blocks a second, the same
 *  as a 1000 tok/s one, and the band's own number is what tells them apart. */
export const STREAM_MAX_HZ = 1000 / TOPUP_MS
export const STREAM_REF_TPS = 1000
/** Never bank more than one tick's worth. A path idle for a minute must not
 *  fire a burst the moment it wakes. */
export const STREAM_CREDIT_MAX = 1

/** tokens/sec -> blocks/sec. Logarithmic and saturating, for the same reason a
 *  meter is not linear: the interesting range spans three decades and the eye
 *  resolves about five events a second. Monotone and pure -- no clock. */
export function blocksPerSec(tps: number | null | undefined): number {
  if (tps == null || !Number.isFinite(tps) || tps < STREAM_MIN_TPS) return 0
  const t = Math.min(1, Math.log(tps / STREAM_MIN_TPS) / Math.log(STREAM_REF_TPS / STREAM_MIN_TPS))
  return Math.min(STREAM_MAX_HZ, STREAM_MIN_HZ + (STREAM_MAX_HZ - STREAM_MIN_HZ) * t)
}

/** One tick of the emission accumulator.
 *
 *  Pure, so the emission RULE is checkable without a clock -- the same reason
 *  `collapseFlows` is a function rather than an inline expression. The credit
 *  is what lets a 3 tok/s band emit at all: rounding a sub-tick rate to zero
 *  every tick would draw nothing forever on a target that is genuinely
 *  serving. */
export function streamCredit(
  prev: number,
  tps: number,
  dtMs: number,
): { credit: number; emit: boolean } {
  const next = Math.min(STREAM_CREDIT_MAX, prev + blocksPerSec(tps) * (dtMs / 1000))
  // The epsilon is not a fudge: a rate of exactly one block per N ticks
  // accumulates N floats that sum to 0.9999999999999999, so the block slips a
  // whole tick every cycle and the slowest bands emit at nine tenths of what
  // was measured. Comparing at binary64's resolution rather than at 1 exactly
  // is what makes the drawn rate the measured one.
  return next >= 1 - 1e-9 ? { credit: Math.max(0, next - 1), emit: true } : { credit: next, emit: false }
}

/** One band's output stream. */
export interface StreamFlow {
  pathKey: string
  /** Measured output tokens per second for this band, off the 1 Hz frame. */
  tokensPerSec: number
  /** Whether this name's RECENT requests mostly streamed. Colour only, and see
   *  `streamTone` for why that is not the same clock as the rate above. Null
   *  when nothing on the wire says either way. */
  streaming: boolean | null
}

/** Same drop-never-redirect rule as `collapseFlows`: a stream naming a path
 *  this layout has no geometry for is dropped, not moved somewhere else. No
 *  summing -- one band is one path, so there is nothing to collapse into. */
export function collapseStreams(
  streams: StreamFlow[],
  paths: Record<string, Point[]>,
): StreamFlow[] {
  return streams.filter((s) => paths[s.pathKey] != null && s.tokensPerSec > 0)
}

/** What share of recent attempts asked for a stream, or null when no row says.
 *  An absent flag is not a false: a coordinator that rolled its rows has no
 *  `streaming` column, and reading that as "nobody streamed" would be an
 *  invention. */
export function streamingRatio(rows: { streaming?: number }[]): number | null {
  const said = rows.filter((r) => r.streaming != null)
  if (said.length === 0) return null
  return said.filter((r) => Boolean(r.streaming)).length / said.length
}

export const STREAM_MAJORITY = 0.5

export function mostlyStreaming(ratio: number | null): boolean | null {
  return ratio == null ? null : ratio >= STREAM_MAJORITY
}

/** The out-stream's colour.
 *
 *  THIS PAINTS A LIVE ANIMATION WITH A HISTORICAL RATIO, and that is a choice
 *  made with its cost known, not an oversight. `GET /api/history/requests` is
 *  the only place that records whether a request asked for `stream: true` --
 *  the 1 Hz frame carries no such flag and `/api/routing` does not split its
 *  `outstanding` by it, so the live wire cannot answer this question at all.
 *
 *  The consequence is real and is not a rounding error: a served name that
 *  streamed all afternoon and is being batch-hammered right now keeps drawing
 *  in the streamed colour until the window rolls past the streamed traffic. So
 *  the two clocks are kept apart everywhere they can be -- a block's PRESENCE
 *  and RATE are live and measured, its COLOUR is how this name has recently
 *  been called -- and the legend names the window so the reader is told the
 *  same thing this comment tells you.
 *
 *  A ratio nobody has rows for is NOT guessed into a default. It paints the
 *  same "no reading" grey the empty meter track uses. */
export function streamTone(streaming: boolean | null): string {
  if (streaming == null) return 'var(--ink-muted)'
  return streaming ? 'var(--stream)' : 'var(--batch)'
}

/** Streamed tokens arrive as slivers; a buffered response arrives whole. The
 *  difference is carried by shape as well as hue, so it survives a
 *  colour-blind reader and a greyscale screenshot. */
export function streamBlockWidth(streaming: boolean | null): number {
  return streaming === true ? 6 : 11
}

/** One flight: a block that slides along `d` over `durationMs`, then removed.
 *  `layer` is never touched by React -- this is the only thing that mutates
 *  it, and it does so with plain DOM calls so the flight keeps running
 *  through any number of parent re-renders. `onDone` fires exactly once, when
 *  the block leaves, so the caller's in-flight tally can drop.
 *
 *  The block is drawn the way the header logo's pulse is: a dashed segment of
 *  the SAME rendered path the wire itself strokes (see `pathsRender` in
 *  layout.ts), animated by sliding `stroke-dashoffset`, rather than a
 *  separate shape whose position is interpolated by hand. That makes it
 *  geometrically impossible for the block to sit off the wire, corners
 *  included, and reads as a lit-up piece of the line rather than a box
 *  riding over it. */
export function spawnParticle(
  layer: SVGGElement,
  d: string,
  color: string,
  durationMs: number = FLIGHT_MS,
  onDone?: () => void,
  /** Dash length, i.e. the block's size ALONG the path. The default is the
   *  inbound request block; a shorter one is how a streamed token reads as a
   *  sliver (`streamBlockWidth`). */
  blockLen: number = 11,
  strokeWidth: number = PARTICLE_STROKE_WIDTH,
): void {
  const NS = 'http://www.w3.org/2000/svg'
  const path = document.createElementNS(NS, 'path')
  path.setAttribute('d', d)
  path.setAttribute('fill', 'none')
  path.setAttribute('stroke', color)
  path.setAttribute('stroke-width', String(strokeWidth))
  // Butt, not round: a round cap adds half the stroke width past each end of
  // the dash, which would elongate the block past `blockLen` -- the same
  // reason the logo's own pulse (docs/screenshots/brand/build.py) uses it.
  path.setAttribute('stroke-linecap', 'butt')
  layer.appendChild(path)

  const total = path.getTotalLength()
  if (!d || total <= 0) {
    path.remove()
    onDone?.()
    return
  }
  path.setAttribute('stroke-dasharray', `${blockLen} ${total}`)

  const t0 = performance.now()
  const span = Math.max(1, durationMs)

  const step = (now: number) => {
    const p = Math.min((now - t0) / span, 1)
    // Slides the dash's leading edge from the path's start (p=0) to past its
    // end (p=1), the same offset technique `pulse_path()` uses.
    path.setAttribute('stroke-dashoffset', String(blockLen - p * (blockLen + total)))
    // A parent unmount can detach `layer` before this flight finishes; the
    // path just keeps walking off-tree for the rest of its span and is
    // garbage the moment the callback stops, so there is nothing to guard.
    if (p < 1) requestAnimationFrame(step)
    else {
      path.remove()
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
  /** The return legs. Empty means nothing selected or nothing being produced,
   *  which draw the same thing: nothing. */
  streams: StreamFlow[]
  paths: Record<string, Point[]>
  /** The same flights, rendered -- what `spawnParticle` actually walks. See
   *  `ClusterLayout.pathsRender` in layout.ts. */
  pathsRender: Record<string, string>
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
    // Fractional emission credit per return leg, owned by this effect for the
    // same reason `active` is: an unmount drops it wholesale rather than
    // letting a remount inherit a backlog.
    const credits = new Map<string, number>()

    const id = window.setInterval(() => {
      const layer = latest.current.layerRef.current
      if (!layer) return
      const byPath = collapseFlows(latest.current.flows, latest.current.paths)
      for (const [key, flow] of byPath) {
        const running = active.get(key) ?? 0
        if (running >= flow.inflight) continue
        const d = latest.current.pathsRender[key]
        if (!d) continue
        active.set(key, running + 1)
        spawnParticle(layer, d, 'var(--flow)', flightMs(flow.meanDurationS), () => {
          active.set(key, Math.max(0, (active.get(key) ?? 1) - 1))
        })
      }

      // The return legs. One loop, one reduced-motion guard, so the output
      // stream is suppressed with the rest of it rather than needing its own.
      for (const s of collapseStreams(latest.current.streams, latest.current.paths)) {
        const d = latest.current.pathsRender[s.pathKey]
        if (!d) continue
        const { credit, emit } = streamCredit(credits.get(s.pathKey) ?? 0, s.tokensPerSec, TOPUP_MS)
        credits.set(s.pathKey, credit)
        // No onDone and no tally: the population is not the measurement here,
        // the rate is, so there is nothing to count back down.
        if (emit) {
          spawnParticle(
            layer,
            d,
            streamTone(s.streaming),
            STREAM_FLIGHT_MS,
            undefined,
            streamBlockWidth(s.streaming),
          )
        }
      }
    }, TOPUP_MS)

    return () => window.clearInterval(id)
  }, [layerRef])
}
