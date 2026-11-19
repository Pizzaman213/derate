import { useState } from 'react'
import { useCandidates, useCluster } from '../../state/resources'
import { useBackend } from '../../state/backend'
import { gbytes, relativeTime, shortGpu } from '../../format'
import { fromState, nodeName } from '../../state/names'
import { PasswordField } from '../../components/PasswordField'
import { Verbatim } from '../../components/Verbatim'

/** Replace the cluster token, behind the signed-in admin's password -- the
 *  same confirm step an admin key's rotate uses. Nothing is revealed: every
 *  member holds its own agent token, and new machines join on an enrollment
 *  token from Add a node. */
function RotateClusterToken() {
  const { backend, invalidate } = useBackend()
  const [open, setOpen] = useState(false)
  const [currentPassword, setCurrentPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      await backend.rotateClusterToken(currentPassword)
      setOpen(false)
      setDone(true)
      setCurrentPassword('')
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ marginTop: 'var(--space-14)' }}>
      <button
        type="button"
        className={open ? undefined : 'ghost'}
        aria-expanded={open}
        onClick={() => {
          setOpen(!open)
          setError(null)
          setDone(false)
        }}
      >
        Rotate cluster token
      </button>
      {done ? <div className="unit">Cluster token rotated. Every member keeps working.</div> : null}
      {open ? (
        <div className="card2" style={{ marginTop: 'var(--space-8)' }}>
          <div className="unit">
            The old cluster token stops admitting new machines at once. Members are not affected: each
            one holds its own agent token. Add machines from now on with an enrollment token.
          </div>
          <div className="fld" style={{ marginTop: 'var(--space-8)' }}>
            <label htmlFor="nc-current-pw">Your password</label>
            <PasswordField
              id="nc-current-pw"
              value={currentPassword}
              autoComplete="current-password"
              onChange={setCurrentPassword}
            />
          </div>
          <div style={{ display: 'flex', gap: 'var(--space-8)', marginTop: 'var(--space-8)' }}>
            <button type="button" disabled={busy || !currentPassword} onClick={() => void run()}>
              {busy ? 'Rotating…' : 'Rotate cluster token'}
            </button>
            <button type="button" className="ghost" disabled={busy} onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
          {error ? (
            <div style={{ color: 'var(--fault)', marginTop: 'var(--space-8)' }}>
              <Verbatim text={error} />
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

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
      <div className="unit" style={{ marginBottom: 'var(--space-10)' }}>
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
              <th>Status</th>
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
                  <td className="unit">
                    {n.healthy ? 'healthy' : <span style={{ color: 'var(--fault)' }}>unreachable</span>}
                  </td>
                  <td className="unit" style={{ color: stale ? 'var(--warn)' : undefined }}>
                    {relativeTime(n.last_seen)}
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    <button
                      type="button"
                      style={{ padding: 'var(--space-3) var(--space-9)' }}
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
        style={{ marginTop: 'var(--space-12)', paddingTop: 'var(--space-10)', borderTop: '1px solid var(--rule)' }}
      >
        Addressable is what the fit gate budgets against, at 90% guardrail. On a unified-memory
        node the operating system shares that pool, so the nameplate total overstates what a
        model can actually have.
      </div>

      {found.length > 0 ? (
        <div style={{ marginTop: 'var(--space-14)', display: 'grid', gap: 'var(--space-8)' }}>
          {found.map((c) => (
            <div
              key={c.node_id}
              style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--space-8)', flexWrap: 'wrap' }}
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
                type="button"
                style={{ padding: 'var(--space-3) var(--space-8)', marginLeft: 'auto' }}
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

      <RotateClusterToken />

      {error ? (
        <div className="label" style={{ color: 'var(--fault)', marginTop: 'var(--space-8)', whiteSpace: 'pre-wrap' }}>
          {error}
        </div>
      ) : null}
    </div>
  )
}
