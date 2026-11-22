// The Fabric settings section's decisions, in a plain module for the same
// reason as `tuning.ts`: `check.mjs` bundles with esbuild `platform: 'neutral'`
// and cannot import anything that pulls in React. `fabric.check.mjs` holds it.

import type { FabricFinding, FabricHealthReport, FabricReach } from '../../api/types'

/** `findings`: something to change. `clean`: both ends read, nothing to
 *  change. `unreadable`: an end could not be asked, and the server said why.
 *  `no-fabric`: both ends answered and one has no RDMA port with a link, so
 *  there is nothing between them to judge -- which is not the same as clean. */
export type FabricState = 'findings' | 'clean' | 'unreadable' | 'no-fabric'

export interface FabricView {
  state: FabricState
  /** Warnings before info, otherwise in the server's order. */
  findings: FabricFinding[]
  /** The server's sentences for whichever end of THIS pair it could not read. */
  reasons: string[]
  /** Per-rail reach rows for this pair, in the server's order; empty when the
   *  pair was not probed (or the coordinator predates the probe). */
  reach: FabricReach[]
}

export const NO_FABRIC =
  'One of these machines has no RDMA port with a link, so there is no fabric between them to check.'

export function fabricView(report: FabricHealthReport | null | undefined, a: string, b: string): FabricView {
  if (!report) return { state: 'no-fabric', findings: [], reasons: [], reach: [] }
  const reasons = report.unreadable.filter((u) => u.node_id === a || u.node_id === b).map((u) => u.reason)
  const link = report.links.find((l) => (l.a === a && l.b === b) || (l.a === b && l.b === a))
  if (link) {
    const rank = (f: FabricFinding) => (f.severity === 'warning' ? 0 : 1)
    // Array.prototype.sort is stable, so equal severities keep the server's order.
    const findings = [...link.findings].sort((x, y) => rank(x) - rank(y))
    return { state: findings.length > 0 ? 'findings' : 'clean', findings, reasons, reach: link.reach ?? [] }
  }
  if (reasons.length > 0) return { state: 'unreadable', findings: [], reasons, reach: [] }
  return { state: 'no-fabric', findings: [], reasons: [], reach: [] }
}

/** "3 warnings · 8 to tune", or null when there is nothing to count. */
export function fabricSummary(view: FabricView): string | null {
  if (view.findings.length === 0) return null
  const warn = view.findings.filter((f) => f.severity === 'warning').length
  const info = view.findings.length - warn
  const parts: string[] = []
  if (warn > 0) parts.push(`${warn} ${warn === 1 ? 'warning' : 'warnings'}`)
  if (info > 0) parts.push(`${info} to tune`)
  return parts.join(' · ')
}

/** "0.12 ms" / "1.4 ms": a connect round trip on this fabric is well under a
 *  millisecond, so two decimals below one and one above. Null stays null. */
export function reachMs(ms: number | null): string | null {
  if (ms == null || !Number.isFinite(ms)) return null
  return `${ms < 1 ? ms.toFixed(2) : ms.toFixed(1)} ms`
}

/** What one reach row says. `connected`: the receiver accepted. Otherwise the
 *  server's error, verbatim -- a refusal still carries a round trip (the far
 *  kernel answered over that rail), silence does not. */
export function reachLine(r: FabricReach): { ok: boolean; ms: string | null; error: string | null } {
  if (r.ok) return { ok: true, ms: reachMs(r.ms), error: null }
  return { ok: false, ms: reachMs(r.ms), error: r.error ?? 'no answer' }
}
