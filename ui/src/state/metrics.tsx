import { createContext, useContext, type ReactNode } from 'react'
import { useMetrics as useMetricsSubscription, type Metrics } from './useMetrics'

// The one place `useMetrics` (the actual SSE subscription) is called. Every
// consumer of live metrics -- the header's stream lamp, the roster, the
// telemetry charts -- reads the same frame through this context instead of
// opening its own EventSource, which is what happened before this file
// existed: one tab's worth of components meant one tab's worth of streams.

const Ctx = createContext<Metrics | null>(null)

export function MetricsProvider({ children }: { children: ReactNode }) {
  const metrics = useMetricsSubscription()
  return <Ctx.Provider value={metrics}>{children}</Ctx.Provider>
}

/** The hoisted metrics subscription. Throws outside `MetricsProvider` rather
 *  than silently returning an empty frame, so a missing provider fails at the
 *  call site instead of rendering a cluster that looks idle. */
export function useMetrics(): Metrics {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useMetrics must be used within a MetricsProvider')
  return ctx
}
