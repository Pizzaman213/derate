import { useEffect, useRef, useState } from 'react'
import type { SafeMetricsFrame } from './useMetrics'

// Exactly nine series, because that is what the Telemetry sub-tab's three
// chart rows draw: cluster (2), per Spark (4, keyed by node_id), per
// deployment (3, keyed by served_name -- the metrics frame only knows
// deployment_id, so the caller supplies the lookup). The control plane keeps
// no history of its own; this is the only place any of it survives longer
// than one frame, and it does that in this browser tab only.

export interface TelemetryPoint {
  t: number
  v: number | null
}

export interface TelemetrySeries {
  clusterTps: TelemetryPoint[]
  clusterPower: TelemetryPoint[]
  nodePower: Record<string, TelemetryPoint[]>
  nodeTemp: Record<string, TelemetryPoint[]>
  nodeMem: Record<string, TelemetryPoint[]>
  nodeUtil: Record<string, TelemetryPoint[]>
  depTps: Record<string, TelemetryPoint[]>
  depTtft: Record<string, TelemetryPoint[]>
  depQueue: Record<string, TelemetryPoint[]>
}

/** Seconds of history kept. The control plane stores one snapshot, not a
 *  trace, so every series starts empty and fills in from here on. */
const WINDOW_S = 60

const EMPTY: TelemetrySeries = {
  clusterTps: [],
  clusterPower: [],
  nodePower: {},
  nodeTemp: {},
  nodeMem: {},
  nodeUtil: {},
  depTps: {},
  depTtft: {},
  depQueue: {},
}

/** Appends one point and trims by TIME, not count, so a stream gap leaves a
 *  real gap in the trace rather than compressing the window when samples
 *  resume. `v: null` is a real point -- "this tick had nothing to say" -- and
 *  is kept rather than skipped, so a chart can draw the gap instead of
 *  silently interpolating across it. */
function push(series: TelemetryPoint[], t: number, v: number | null): TelemetryPoint[] {
  const next = [...series, { t, v }]
  const cutoff = t - WINDOW_S
  return next.filter((p) => p.t >= cutoff)
}

/** Same as `push`, across every key ever seen (plus any new this frame). A
 *  key that existed before but is missing from this frame still gets a
 *  `null` point, so its chart shows a gap instead of quietly stalling on the
 *  last value it had. */
function pushKeyed(
  map: Record<string, TelemetryPoint[]>,
  keys: Iterable<string>,
  t: number,
  valueFor: (key: string) => number | null,
): Record<string, TelemetryPoint[]> {
  const out: Record<string, TelemetryPoint[]> = {}
  for (const key of keys) {
    out[key] = push(map[key] ?? [], t, valueFor(key))
  }
  return out
}

export function useTelemetry(
  frame: SafeMetricsFrame | null,
  servedNameFor: (deploymentId: string) => string | null | undefined,
): TelemetrySeries {
  const [series, setSeries] = useState<TelemetrySeries>(EMPTY)
  const lastTsRef = useRef<number | null>(null)
  // The lookup can be a fresh closure every render (it usually is -- built
  // from whatever cluster/topology resource the caller holds); a ref keeps
  // the accumulation effect keyed on the frame alone.
  const servedNameForRef = useRef(servedNameFor)
  servedNameForRef.current = servedNameFor

  useEffect(() => {
    if (!frame) return
    if (lastTsRef.current === frame.ts) return
    lastTsRef.current = frame.ts
    const t = frame.ts

    setSeries((prev) => {
      const nodeIds = new Set(Object.keys(prev.nodePower))
      const nodeById = new Map(frame.nodes.map((n) => [n.node_id, n]))
      for (const id of nodeById.keys()) nodeIds.add(id)

      const depNames = new Set(Object.keys(prev.depTps))
      const depByName = new Map<string, SafeMetricsFrame['deployments'][number]>()
      for (const d of frame.deployments) {
        const name = servedNameForRef.current(d.deployment_id)
        if (!name) continue
        depNames.add(name)
        depByName.set(name, d)
      }

      return {
        clusterTps: push(prev.clusterTps, t, frame.cluster.tokens_per_sec),
        clusterPower: push(prev.clusterPower, t, frame.cluster.total_power_w),
        nodePower: pushKeyed(prev.nodePower, nodeIds, t, (id) => nodeById.get(id)?.power_w ?? null),
        nodeTemp: pushKeyed(prev.nodeTemp, nodeIds, t, (id) => nodeById.get(id)?.temp_c ?? null),
        nodeMem: pushKeyed(
          prev.nodeMem,
          nodeIds,
          t,
          (id) => nodeById.get(id)?.memory_used_pct ?? null,
        ),
        nodeUtil: pushKeyed(prev.nodeUtil, nodeIds, t, (id) => nodeById.get(id)?.util_pct ?? null),
        depTps: pushKeyed(
          prev.depTps,
          depNames,
          t,
          (name) => depByName.get(name)?.tokens_per_sec ?? null,
        ),
        depTtft: pushKeyed(
          prev.depTtft,
          depNames,
          t,
          (name) => depByName.get(name)?.ttft_ms ?? null,
        ),
        depQueue: pushKeyed(
          prev.depQueue,
          depNames,
          t,
          (name) => depByName.get(name)?.queue_depth ?? null,
        ),
      }
    })
  }, [frame])

  return series
}
