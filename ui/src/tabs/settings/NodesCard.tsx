import { useState } from 'react'
import { useCandidates, useCluster } from '../../state/resources'
import { useBackend } from '../../state/backend'
import { gbytes, relativeTime, shortGpu } from '../../format'
import { fromState, nodeName } from '../../state/names'

// Ported from mockups-next/js/settings.js `settings()`'s `nodeTable` plus
// derate.html's "discovered" row. The mockup's own address/kind form is not
// carried over: join is worker-to-coordinator and token-gated, so there is
// no endpoint an address field could call, and its accompanying "Not built
// yet" card says so -- the form there is illustrative, not a promise.
export function NodesCard() {
  const cluster = useCluster()
  const candidates = useCandidates()
  const { backend, invalidate } = useBackend()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const nodes = cluster.data?.nodes ?? []
  const found = candidates.data ?? []

  const remove = async (nodeId: string) => {
    if (!window.confirm(`Remove ${nodeId} from the cluster?`)) return
    setBusy(nodeId)
    setError(null)
    try {
      await backend.removeNode(nodeId)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const admit = async (nodeId: string) => {
    setBusy(nodeId)
    setError(null)
    try {
      await backend.admit(nodeId)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="card2">
      <h3>Nodes</h3>
      <div className="unit" style={{ marginBottom: 10 }}>
        Nodes on the local subnet are discovered automatically.
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Node</th>
              <th>Address</th>
              <th>Hardware</th>
              <th style={{ textAlign: 'right' }}>Memory</th>
              <th style={{ textAlign: 'right' }}>Bandwidth</th>
              <th>Driver</th>
              <th>Seen</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {nodes.map((n) => {
              const p = n.profile
              const stale = n.state !== 'healthy'
              return (
                <tr key={p.node_id}>
                  <td className="mono">
                    {nodeName(fromState(n))}
                    {/* The id everything else is keyed by, kept beside a chosen
                        name so this table can still be read against a
                        deployment's node list. */}
                    {nodeName(fromState(n)) !== p.node_id ? (
                      <span className="unit"> {p.node_id}</span>
                    ) : null}
                    {n.role === 'coordinator' ? <span className="unit"> coordinator</span> : null}
                  </td>
                  <td className="mono unit">{p.address}</td>
                  <td className="unit">
                    {shortGpu(p.gpu_name)} <span className="muted">· sm_{p.compute_capability}</span>
                  </td>
                  <td className="num">
                    {gbytes(p.addressable_memory, 1)}{' '}
                    <span className="unit">of {gbytes(p.total_memory, 0)} GiB</span>
                  </td>
                  <td className="num">
                    {p.memory_bandwidth_gbps.toFixed(0)} <span className="unit">GB/s</span>
                  </td>
                  <td className="unit mono">{p.driver_version}</td>
                  <td className="unit" style={{ color: stale ? 'var(--warn)' : undefined }}>
                    {relativeTime(n.last_seen)}
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    <button
                      style={{ padding: '3px 9px' }}
                      onClick={() => void remove(p.node_id)}
                      disabled={busy === p.node_id}
                    >
                      {busy === p.node_id ? 'Removing…' : 'Remove'}
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div
        className="unit"
        style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--rule)' }}
      >
        Addressable is what the fit gate budgets against, at 90% guardrail. On a unified-memory
        node the operating system shares that pool, so the nameplate total overstates what a
        model can actually have.
      </div>

      {found.length > 0 ? (
        <div style={{ marginTop: 14, display: 'grid', gap: 8 }}>
          {found.map((c) => (
            <div
              key={c.node_id}
              style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}
            >
              <span className="pill">discovered</span>
              <span className="mono">{c.hostname}</span>
              <span className="unit">
                {shortGpu(c.gpu_name)} · {gbytes(c.addressable_memory, 0)} GB · {c.address}
              </span>
              {c.eligible === false && c.ineligible_reason ? (
                <span className="unit">{c.ineligible_reason}</span>
              ) : null}
              <button
                style={{ padding: '3px 8px', marginLeft: 'auto' }}
                onClick={() => void admit(c.node_id)}
                /* Not gated on c.eligible. The reason above says the device
                   class could not be confirmed, and serialize._eligibility is
                   explicit that this is "cannot confirm" rather than an
                   exclusion "the system does not enforce" -- nothing in the
                   planner or the fit gate filters placement on device class.
                   Disabling the button turned that hedge into a refusal, and
                   it is the refusal that a GPU-less machine -- a Mac, a Pi --
                   hits on the one path a human drives. The operator is told
                   what we could not confirm and admits anyway. */
                disabled={busy === c.node_id}
              >
                {busy === c.node_id ? 'Adding…' : 'Admit'}
              </button>
            </div>
          ))}
        </div>
      ) : null}

      {error ? (
        <div className="label" style={{ color: 'var(--fault)', marginTop: 8, whiteSpace: 'pre-wrap' }}>
          {error}
        </div>
      ) : null}
    </div>
  )
}
