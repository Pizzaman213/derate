import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { useTelemetry, type TelemetrySeries } from './useTelemetry'
import { useMetrics } from './metrics'
import { useCluster } from './resources'

// The one place `useTelemetry` (the actual 60-second accumulation) is called,
// for the same reason `metrics.tsx` exists for the SSE subscription: this is
// STATEFUL, and calling it twice means two windows that fill independently.
//
// It used to be called in DashboardTab and prop-drilled from there, which made
// the accumulated history reachable only from that one destination. The node
// sheet is not inside the Dashboard, so a chart in it would have started an
// empty 60-second window every time somebody opened a machine -- a graph that
// is blank for a minute after every click is not a graph.
//
// Mounted above AppShell, so the window keeps filling whichever destination is
// showing and whether or not any sheet is open.

const Ctx = createContext<TelemetrySeries | null>(null)

export function TelemetryProvider({ children }: { children: ReactNode }) {
  const { frame } = useMetrics()
  const cluster = useCluster()

  // The metrics frame knows deployment ids; every series here is keyed by
  // served name, because that is what a person selects and what the sheet and
  // the deployments strip both address a deployment by.
  const servedNameById = useMemo(() => {
    const m = new Map<string, string>()
    for (const d of cluster.data?.deployments ?? []) m.set(d.deployment_id, d.served_name)
    return m
  }, [cluster.data])

  const series = useTelemetry(frame, (id) => servedNameById.get(id))
  return <Ctx.Provider value={series}>{children}</Ctx.Provider>
}

/** The hoisted 60-second window. Throws outside `TelemetryProvider` rather
 *  than returning an empty series, so a missing provider fails at the call
 *  site instead of rendering charts that would never fill. */
export function useTelemetrySeries(): TelemetrySeries {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useTelemetrySeries must be used within a TelemetryProvider')
  return ctx
}
