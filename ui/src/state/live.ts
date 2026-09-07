// Node "is it actually healthy right now" logic, factored out of the roster
// panel so the node detail view can share it exactly rather than reimplement
// it (views/NodeDetail.tsx imports both functions from here, by way of
// panels/NodeRoster.tsx re-exporting them for the old call site).

import type { NodeStateDTO } from '../api/types'
import type { SafeMetricsFrame } from './useMetrics'

/** Presentation thresholds for the warn lamp. Pressure, not failure: the node
 *  is serving, and it is close to a limit somebody should know about. */
export const HOT_C = 80
export const MEMORY_PRESSURE_PCT = 92

export interface NodeLive {
  power_w: number | null
  temp_c: number | null
  memory_used_pct: number | null
  util_pct: number | null
}

/** Live values for a node, falling back to the last snapshot when the stream
 *  has nothing. Returns `fresh: false` when the numbers are last-known rather
 *  than current, so the caller greys them instead of passing them off as live. */
export function nodeLive(
  node: NodeStateDTO,
  frame: SafeMetricsFrame | null,
  streamStale: boolean,
): NodeLive & { fresh: boolean } {
  const f = frame?.nodes.find((n) => n.node_id === node.profile.node_id)
  const hasLive =
    !streamStale &&
    f != null &&
    (f.power_w != null || f.temp_c != null || f.util_pct != null)

  if (hasLive) {
    return {
      power_w: f.power_w,
      temp_c: f.temp_c,
      memory_used_pct: f.memory_used_pct,
      util_pct: f.util_pct,
      fresh: true,
    }
  }
  return {
    power_w: node.power_watts,
    temp_c: node.temperature_c,
    memory_used_pct: (node.memory_used / node.profile.addressable_memory) * 100,
    util_pct: node.utilization_pct,
    fresh: false,
  }
}

export function nodeSignal(
  node: NodeStateDTO,
  live: NodeLive,
): 'live' | 'warn' | 'fault' {
  if (node.state === 'unreachable' || !node.healthy) return 'fault'
  if ((live.temp_c ?? 0) >= HOT_C) return 'warn'
  if ((live.memory_used_pct ?? 0) >= MEMORY_PRESSURE_PCT) return 'warn'
  if (node.state === 'degraded') return 'warn'
  return 'live'
}
