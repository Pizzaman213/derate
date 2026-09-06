import { useEffect, useRef, useState } from 'react'
import type { MetricsDeploymentFrame, MetricsFrame, MetricsNodeFrame } from '../api/types'
import type { StreamState } from '../api/client'
import { useBackend } from './backend'

/** Seconds of throughput history the sparkline draws. */
export const HISTORY_SECONDS = 60

/** `MetricsFrame` with `nodes`/`deployments` coalesced to arrays. This is the
 *  only shape the rest of the UI should ever see — the null-on-degraded-path
 *  wire honesty lives in `MetricsFrame` and is resolved right here, once, so
 *  every consumer's `.find`/`.filter` is safe without a guard of its own. */
export type SafeMetricsFrame = Omit<MetricsFrame, 'nodes' | 'deployments'> & {
  nodes: MetricsNodeFrame[]
  deployments: MetricsDeploymentFrame[]
}

export interface Metrics {
  frame: SafeMetricsFrame | null
  /** Last HISTORY_SECONDS of cluster tokens/sec, oldest first. */
  history: { t: number; v: number }[]
  stream: StreamState
  /** True while the stream is down. Live values render greyed, not frozen and
   *  not zeroed: a frozen number that looks live is worse than an obviously
   *  stale one. */
  stale: boolean
}

export function useMetrics(): Metrics {
  const { backend } = useBackend()
  const [frame, setFrame] = useState<SafeMetricsFrame | null>(null)
  const [stream, setStream] = useState<StreamState>({ status: 'connecting' })
  const historyRef = useRef<{ t: number; v: number }[]>([])
  const [history, setHistory] = useState<{ t: number; v: number }[]>([])

  useEffect(() => {
    if (!backend) return
    return backend.subscribe(
      (f) => {
        setFrame({ ...f, nodes: f.nodes ?? [], deployments: f.deployments ?? [] })
        const v = f.cluster.tokens_per_sec
        if (v != null) {
          const next = [...historyRef.current, { t: f.ts, v }]
          // Trim by time, not by count, so a stream gap leaves a real gap in the
          // trace rather than compressing it.
          const cutoff = f.ts - HISTORY_SECONDS
          historyRef.current = next.filter((p) => p.t >= cutoff)
          setHistory(historyRef.current)
        }
      },
      (s) => setStream(s),
    )
  }, [backend])

  return { frame, history, stream, stale: stream.status === 'stale' }
}
