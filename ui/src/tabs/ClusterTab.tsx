import { useRef, useState } from 'react'
import { useCluster, useRouting, useTopology } from '../state/resources'
import { useMetrics } from '../state/metrics'
import { useBackend } from '../state/backend'
import { FlowGraph, type FlowGraphHandle } from './cluster/FlowGraph'
import { GraphToolbar } from './cluster/GraphToolbar'
import { SelectionRail } from './cluster/SelectionRail'

/** The Cluster destination: the request-flow graph and the rail beneath it.
 *
 *  Ported from mockups-next/js/cluster.js + inspectors.js. This component
 *  fetches its own data (the same `state/resources` hooks every other
 *  destination uses) rather than taking props, so it can be dropped into
 *  AppShell's `cluster` tabpanel on its own. */
export function ClusterTab() {
  const cluster = useCluster()
  const topology = useTopology()
  const routing = useRouting()
  const { frame, stale } = useMetrics()
  const { backend, invalidate } = useBackend()

  const [measuring, setMeasuring] = useState<string | null>(null)
  const [measureError, setMeasureError] = useState<{ key: string; message: string } | null>(null)
  const graphRef = useRef<FlowGraphHandle>(null)
  const zoomLabelRef = useRef<HTMLSpanElement>(null)

  const measure = async (a: string, b: string) => {
    const key = [a, b].sort().join('~')
    setMeasuring(key)
    setMeasureError(null)
    try {
      await backend.measureLink(a, b)
      invalidate()
    } catch (err) {
      // A rejected measurement must surface somewhere, not vanish into an
      // unhandled promise rejection while the button silently re-enables
      // and the rail still reads "Never measured" with no explanation.
      setMeasureError({ key, message: err instanceof Error ? err.message : 'Measurement failed.' })
    } finally {
      setMeasuring(null)
    }
  }

  return (
    <div className="stage bare">
      <GraphToolbar graphRef={graphRef} zoomLabelRef={zoomLabelRef} />

      <FlowGraph
        ref={graphRef}
        deployments={cluster.data?.deployments ?? []}
        topology={topology.data ?? { cluster_id: '', coordinator: '', nodes: [], edges: [], deployments: [] }}
        nodes={cluster.data?.nodes ?? []}
        routing={routing.data ?? []}
        zoomLabelRef={zoomLabelRef}
      />

      <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--rule)' }}>
        <SelectionRail
          edges={topology.data?.edges ?? []}
          measurements={cluster.data?.links ?? []}
          nodes={cluster.data?.nodes ?? []}
          frame={frame}
          stale={stale}
          measuring={measuring}
          measureError={measureError}
          onMeasure={(a, b) => void measure(a, b)}
        />
      </div>

      <div className="legend" style={{ marginTop: 11, paddingTop: 11, borderTop: '1px solid var(--rule)' }}>
        <span>
          <b>heavy</b> measured, on your hardware
        </span>
        <span>
          <b>thin dashed</b> crosses the routing boundary, no measured fabric
        </span>
        <span>
          <b>hairline</b> reachable through the proxy, not routed there
        </span>
        <span>
          <b>faded</b> idle
        </span>
        <span>
          <b>blue</b> request in flight
        </span>
        <span>
          <b>outlined</b> third party
        </span>
      </div>
    </div>
  )
}
