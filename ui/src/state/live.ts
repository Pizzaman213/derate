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

/** How old a node's own telemetry sample may get before its live readings stop
 *  counting as live. The coordinator samples every second and the metrics frame
 *  is published on its own interval, so a handful of missed samples is noise;
 *  a node that has not produced one in this long has a dead telemetry source,
 *  not a slow one. The canonical case is a container started without `--gpus`:
 *  its agent answers /agent/health forever, so `last_seen` stays fresh while
 *  power, temperature and utilisation are frozen at whatever they read when
 *  nvidia-smi was last reachable. */
export const SAMPLE_STALE_S = 30

/** What the utilisation readout is actually measuring on this machine.
 *
 *  A node the probe found no GPU on reports CPU utilisation from /proc/stat --
 *  the gateway sends the host figure rather than leaving the tile at a
 *  permanent 0%. Naming that "GPU utilisation" would label hardware the node
 *  does not have, which is worse than the zero it replaced. */
export function utilLabel(profile: { gpu_count: number }): string {
  return profile.gpu_count === 0 ? 'CPU utilisation' : 'GPU utilisation'
}

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
  // The frame arriving does not mean this node is in it. Its sample carries its
  // own age, measured on the coordinator's clock like the frame's `ts`, so a
  // node whose telemetry died is caught even while the stream is healthy.
  const sampleTs = f?.sample_ts ?? node.sample_ts ?? null
  const sampleFresh =
    frame != null && sampleTs != null && frame.ts - sampleTs <= SAMPLE_STALE_S
  const hasLive =
    !streamStale &&
    sampleFresh &&
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
  // Addressable first: on a GPU node that is the pool anything can be planned
  // into. A node that probed as UNKNOWN -- no nvidia-smi, so no GPU name and no
  // GPU memory at all -- has none, and falls back to the live host total from
  // its own sample. Both can be 0, and dividing by 0 yields Infinity, which
  // renders as a percentage and reads as a catastrophic reading rather than as
  // the absent one it is, so the guard stays.
  const denominator = node.profile.addressable_memory || node.memory_total
  return {
    power_w: node.power_watts,
    temp_c: node.temperature_c,
    memory_used_pct:
      denominator > 0 ? (node.memory_used / denominator) * 100 : null,
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
