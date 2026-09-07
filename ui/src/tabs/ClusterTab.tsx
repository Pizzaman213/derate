import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReachReport } from '../api/types'
import { useCluster, useRouting, useTopology } from '../state/resources'
import { useBackend } from '../state/backend'
import { nameIndex } from '../state/names'
import { ClusterGraph, type ClusterGraphHandle } from './cluster/ClusterGraph'
import { GraphToolbar } from './cluster/GraphToolbar'
import { SelectionRail } from './cluster/SelectionRail'
import { clearOrder, readOrder, writeOrder } from './cluster/order'

const EMPTY_TOPOLOGY = { cluster_id: '', coordinator: '', nodes: [], edges: [], deployments: [] }

/** The Cluster destination: the machine floor and the rail beneath it.
 *
 *  This component fetches its own data (the same `state/resources` hooks every
 *  other destination uses) rather than taking props, so it can be dropped into
 *  AppShell's `cluster` tabpanel on its own. */
export function ClusterTab() {
  const cluster = useCluster()
  const topology = useTopology()
  const routing = useRouting()
  const { backend, invalidate } = useBackend()

  const [measuring, setMeasuring] = useState<string | null>(null)
  const [measureError, setMeasureError] = useState<{ key: string; message: string } | null>(null)
  // The reachability check is separate state from the measurement above, and
  // deliberately so: they are different questions at different prices, and one
  // running must not grey out the other's button.
  const [checking, setChecking] = useState<string | null>(null)
  const [reachReport, setReachReport] = useState<{ key: string; value: ReachReport } | null>(null)
  const [reachError, setReachError] = useState<{ key: string; message: string } | null>(null)
  const graphRef = useRef<ClusterGraphHandle>(null)
  const zoomLabelRef = useRef<HTMLSpanElement>(null)

  const clusterId = topology.data?.cluster_id ?? ''
  const [order, setOrder] = useState<string[] | null>(null)
  const [live, setLive] = useState('')

  // The arrangement is per cluster, and the cluster id is not known until the
  // first poll answers.
  useEffect(() => {
    setOrder(clusterId ? readOrder(clusterId) : null)
  }, [clusterId])

  const reorder = useCallback(
    (next: string[]) => {
      setOrder(next)
      if (clusterId) writeOrder(clusterId, next)
    },
    [clusterId],
  )

  const resetLayout = useCallback(() => {
    setOrder(null)
    if (clusterId) clearOrder(clusterId)
    setLive('Machines put back in their default order.')
  }, [clusterId])

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

  const checkReach = async (a: string, b: string) => {
    const key = [a, b].sort().join('~')
    setChecking(key)
    setReachError(null)
    try {
      setReachReport({ key, value: await backend.checkReach(a, b) })
    } catch (err) {
      // A refused check has to say so where the button is. The alternative --
      // the button re-enabling with nothing new on screen -- reads as "checked,
      // and everything is fine", which is the one thing it does not mean.
      setReachReport(null)
      setReachError({ key, message: err instanceof Error ? err.message : 'The check failed.' })
    } finally {
      setChecking(null)
    }
  }

  // One naming rule for the whole destination, so a plate and the chip naming
  // the same machine cannot disagree.
  const name = useMemo(() => nameIndex(topology.data?.nodes ?? []), [topology.data])

  const nodeCount = topology.data?.nodes.length ?? 0

  return (
    <div className="stage bare clusterstage">
      <GraphToolbar
        graphRef={graphRef}
        zoomLabelRef={zoomLabelRef}
        rearranged={order != null}
        onResetLayout={resetLayout}
      />

      {/* The drawing takes whatever is left between the toolbar and the two
          reference blocks below, and sits in the middle of it. */}
      <div className="floor">
        <ClusterGraph
          ref={graphRef}
          deployments={cluster.data?.deployments ?? []}
          topology={topology.data ?? EMPTY_TOPOLOGY}
          nodes={cluster.data?.nodes ?? []}
          routing={routing.data ?? []}
          zoomLabelRef={zoomLabelRef}
          order={order}
          onReorder={reorder}
          announce={setLive}
        />
      </div>

      <p aria-live="polite" className="sr-only">
        {live}
      </p>

      {/* The rail and the legend are the reference desk, so .clusterstage pins
          them to the bottom of the destination instead of letting them trail
          the drawing. Nothing above them moves when a link is selected. */}
      <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--rule)' }}>
        <SelectionRail
          edges={topology.data?.edges ?? []}
          measurements={cluster.data?.links ?? []}
          measuring={measuring}
          measureError={measureError}
          onMeasure={(a, b) => void measure(a, b)}
          name={name}
          reach={{
            checking,
            report: reachReport,
            error: reachError,
            onCheck: (a, b) => void checkReach(a, b),
          }}
          crowded={nodeCount > 4}
        />
      </div>

      {/* One meaning per channel. Width is measured bandwidth; a dash is
          never-measured or a boundary crossing; colour means something is
          wrong, which is why a healthy plate has none. */}
      <div className="legend" style={{ marginTop: 11, paddingTop: 11, borderTop: '1px solid var(--rule)' }}>
        <span>
          <b>thick</b> measured all-reduce, against the 40 GB/s tensor-parallel threshold
        </span>
        <span>
          <b>dashed</b> never measured, and carrying no figure
        </span>
        <span>
          <b>inset meter</b> memory used
        </span>
        <span>
          <b>plate border</b> amber or red when the machine needs looking at
        </span>
        <span>
          <b>band</b> one deployment across the machines it occupies
        </span>
        <span>
          <b>thin dashed</b> crosses the routing boundary, no measured fabric
        </span>
        <span>
          <b>outlined</b> third party
        </span>
        <span>
          <b>blue</b> request in flight
        </span>
      </div>
    </div>
  )
}
