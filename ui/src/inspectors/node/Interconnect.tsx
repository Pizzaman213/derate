import { useState } from 'react'
import type { LinkMeasurement, TopologyEdge } from '../../api/types'
import { useBackend } from '../../state/backend'
import { useCluster, useTopology } from '../../state/resources'
import { edgeMeasured } from '../../tabs/cluster/layout'
import { fmtUnit, relativeTime } from '../../format'

/** This machine's links to every peer.
 *
 *  The rule is SelectionRail's, unchanged: an unmeasured pair carries no
 *  numbers at all -- not an estimate, not a zero, not a greyed-out figure from
 *  a different pair. Width means measured bandwidth and a dash means never
 *  measured, and there is nothing else those two channels are allowed to say.
 *
 *  Nothing new is fetched: `Cluster.links` is already polled every five
 *  seconds, and `/api/topology`'s edges carry the `measured`/`stale` flags. */
export function Interconnect({ nodeId }: { nodeId: string }) {
  const cluster = useCluster()
  const topology = useTopology()
  const { backend, invalidate } = useBackend()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<{ peer: string; message: string } | null>(null)

  const peers = (cluster.data?.nodes ?? [])
    .map((n) => n.profile.node_id)
    .filter((id) => id !== nodeId)

  if (cluster.data == null) return <div className="unit">Reading…</div>
  if (peers.length === 0) {
    return (
      <div className="unit">
        The only machine in the cluster, so there is no link to measure.
      </div>
    )
  }

  const measure = async (peer: string) => {
    setBusy(peer)
    setError(null)
    try {
      await backend.measureLink(nodeId, peer)
      invalidate()
    } catch (e) {
      setError({ peer, message: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      {peers.map((peer) => {
        const edge = findEdge(topology.data?.edges ?? [], nodeId, peer)
        const link = findLink(cluster.data?.links ?? [], nodeId, peer)
        const measured = edgeMeasured(edge)
        return (
          <div key={peer} className="row">
            <span className="mono">{peer}</span>
            <span style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              {measured ? (
                <span className="unit">
                  {fmtUnit(edge?.all_reduce_gbps, 1, 'GB/s all-reduce')}
                  {link?.latency_us != null ? ` · ${fmtUnit(link.latency_us, 0, 'µs')}` : ''}
                  {link?.measured_at ? ` · ${relativeTime(link.measured_at)}` : ''}
                  {edge?.stale ? ' · stale' : ''}
                </span>
              ) : (
                <span className="unit">never measured</span>
              )}
              <button onClick={() => void measure(peer)} disabled={busy === peer}>
                {busy === peer ? 'Measuring…' : measured ? 'Re-measure' : 'Measure'}
              </button>
            </span>
          </div>
        )
      })}
      {error ? (
        <p
          className="label"
          style={{ color: 'var(--fault)', fontWeight: 400, margin: '8px 0 0' }}
        >
          {error.message}
        </p>
      ) : null}
    </>
  )
}

function findEdge(edges: TopologyEdge[], a: string, b: string): TopologyEdge | undefined {
  return edges.find((e) => (e.src === a && e.dst === b) || (e.src === b && e.dst === a))
}

function findLink(links: LinkMeasurement[], a: string, b: string): LinkMeasurement | undefined {
  return links.find((l) => (l.src === a && l.dst === b) || (l.src === b && l.dst === a))
}
