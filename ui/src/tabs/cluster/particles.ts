// Imperative rAF particle layer. Ports mockups-next/js/cluster.js's fly() --
// the 1600ms flight along a path, the emission cadence -- into a React ref
// this component never reconciles: a dot's position is a DOM attribute write
// every frame, not state, so a request in flight survives a pan/zoom and
// costs nothing in the render tree.
//
// The one thing NOT ported verbatim is which weights choose the path. The
// mockup's fly() reads mockups-next/js/routing.js's curW(), which recomputes
// a share client-side per policy. That is exactly what "NO client-computed
// routing weights" forbids -- pickPath below reads RouteTarget.weight
// straight off the wire and nothing else.

import { useEffect, useRef, type RefObject } from 'react'
import type { RoutingConfig } from '../../api/types'
import type { Point } from './layout'

export const FLIGHT_MS = 1600
export const EMIT_MS = 380

/** Resolves the selected deployment's routing targets onto whichever flight
 *  paths this graph actually drew, and picks one by wire weight.
 *
 *  Candidates are the local hops (`#L0`, `#L1`, ...) plus the provider tap
 *  (`#P`) when one was drawn. When the routing config lists exactly as many
 *  targets as there are candidate paths, each target's real `weight` drives
 *  the draw, in the same order (local targets first, matching how layout.ts
 *  emits `#L` paths in `node_ids` order, then remote). When the counts don't
 *  line up -- a target spanning the whole pipeline as one indivisible unit
 *  rather than one target per node, say -- every candidate gets an even
 *  share instead of guessing which target it corresponds to. Either way,
 *  nothing here invents a specific number; it only ever draws one of the
 *  paths this graph already has geometry for. */
export function pickPath(
  servedName: string,
  cfg: RoutingConfig | null,
  paths: Record<string, Point[]>,
): Point[] | null {
  const localKeys = Object.keys(paths)
    .filter((k) => k.startsWith(`${servedName}#L`))
    .sort((a, b) => Number(a.slice(a.indexOf('#L') + 2)) - Number(b.slice(b.indexOf('#L') + 2)))
  const providerKey = `${servedName}#P`
  const candidates = paths[providerKey] ? [...localKeys, providerKey] : localKeys
  if (candidates.length === 0) return null

  const weights =
    cfg && cfg.targets.length === candidates.length
      ? cfg.targets.map((t) => Math.max(0, t.weight))
      : candidates.map(() => 1)

  const total = weights.reduce((a, b) => a + b, 0)
  if (total <= 0) return paths[candidates[0]!] ?? null

  let r = Math.random() * total
  for (let i = 0; i < candidates.length; i++) {
    r -= weights[i]!
    if (r <= 0) return paths[candidates[i]!] ?? null
  }
  return paths[candidates[candidates.length - 1]!] ?? null
}

/** One flight: a small rect walked along `pts` over FLIGHT_MS, then removed.
 *  `layer` is never touched by React -- this is the only thing that mutates
 *  it, and it does so with plain DOM calls so the flight keeps running
 *  through any number of parent re-renders. */
export function spawnParticle(layer: SVGGElement, pts: Point[], color: string): void {
  if (pts.length < 2) return
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

  const step = (now: number) => {
    const p = Math.min((now - t0) / FLIGHT_MS, 1)
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
    // rect just keeps walking off-tree for the rest of its 1.6s and is
    // garbage the moment the callback stops, so there is nothing to guard.
    if (p < 1) requestAnimationFrame(step)
    else box.remove()
  }
  requestAnimationFrame(step)
}

export interface ParticleFieldOptions {
  layerRef: RefObject<SVGGElement>
  /** The selected deployment is actually doing something -- frame tokens/sec
   *  or outstanding requests are nonzero. Idle emits nothing. */
  hasTraffic: boolean
  servedName: string | null
  cfg: RoutingConfig | null
  paths: Record<string, Point[]>
}

/** Mount-scoped emission at the mockup's 380ms cadence, cancelled on unmount
 *  and fully suppressed under prefers-reduced-motion -- flight is decoration
 *  about a real event (there is traffic), not a value read off a chart, so a
 *  fixed cadence while `hasTraffic` holds is honest even though the interval
 *  itself is not a measurement. */
export function useParticleField(opts: ParticleFieldOptions): void {
  const { layerRef, hasTraffic, servedName } = opts
  const latest = useRef(opts)
  latest.current = opts

  useEffect(() => {
    if (!hasTraffic || !servedName) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return

    const id = window.setInterval(() => {
      const layer = latest.current.layerRef.current
      if (!layer) return
      const pts = pickPath(latest.current.servedName!, latest.current.cfg, latest.current.paths)
      if (!pts) return
      spawnParticle(layer, pts, 'var(--flow)')
    }, EMIT_MS)

    return () => window.clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- cfg/paths read via `latest`
  }, [hasTraffic, servedName, layerRef])
}
