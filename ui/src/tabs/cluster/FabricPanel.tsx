import { useCallback, useEffect, useState } from 'react'
import type { FabricHealthReport } from '../../api/types'
import { Copyable } from '../../components/Copyable'
import { Verbatim } from '../../components/Verbatim'
import { relativeTime } from '../../format'
import { useBackend } from '../../state/backend'
import { NO_FABRIC, fabricSummary, fabricView, reachLine } from './fabric'

/** "Fabric settings" under a selected link: what to change on each end's RDMA
 *  ports, from `GET /api/links/health?a=&b=`.
 *
 *  Read when the link is selected and on demand, never polled: it is a handful
 *  of sysfs reads and `ethtool` queries per end, cheap but not free, and a
 *  setting does not change while nobody is at the shell. Each finding's
 *  sentence is the server's and renders verbatim; its fix is the exact command
 *  or netplan stanza, in a copyable block, because it is run somewhere else. */
export function FabricPanel({ a, b, name }: { a: string; b: string; name: (id: string) => string }) {
  const { backend } = useBackend()
  const key = [a, b].sort().join('~')
  // Kept keyed by pair so a reply for the previous link never renders here.
  const [report, setReport] = useState<{ key: string; value: FabricHealthReport } | null>(null)
  const [error, setError] = useState<{ key: string; message: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const read = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const value = await backend.linkHealth(a, b)
      setReport({ key, value })
    } catch (err) {
      setError({ key, message: err instanceof Error ? err.message : 'The read failed.' })
    } finally {
      setBusy(false)
    }
  }, [backend, a, b, key])

  useEffect(() => {
    void read()
  }, [read])

  const current = report?.key === key ? report.value : null
  const view = fabricView(current, a, b)
  const summary = fabricSummary(view)

  return (
    <div style={{ marginTop: 'var(--space-10)' }}>
      <div className="sub">Fabric settings</div>
      <div className="row">
        <span className="unit">
          {busy && !current
            ? 'Reading both ends…'
            : view.state === 'clean'
              ? 'Nothing to change on either end.'
              : (summary ?? '')}
          {current ? ` · read ${relativeTime(current.measured_at)}` : ''}
        </span>
        <button type="button" className="ghost" onClick={() => void read()} disabled={busy}>
          {busy ? 'Reading…' : 'Read again'}
        </button>
      </div>

      {error?.key === key && !busy ? <Verbatim text={error.message} size="unit" /> : null}
      {current && view.state === 'no-fabric' ? <div className="unit">{NO_FABRIC}</div> : null}
      {view.reasons.map((r) => (
        <Verbatim key={r} text={r} size="unit" />
      ))}

      {view.reach.length > 0 ? (
        <div style={{ marginTop: 'var(--space-10)' }}>
          <div className="label">Rail reach · a connect to the copy receiver from each rail</div>
          {view.reach.map((r) => {
            const line = reachLine(r)
            return (
              <div
                key={`${r.node_id}~${r.iface}~${r.target}`}
                style={{
                  borderLeft: `var(--space-2) solid ${line.ok || line.ms != null ? 'var(--rule)' : 'var(--fault)'}`,
                  paddingLeft: 'var(--space-6)',
                  marginTop: 'var(--space-4)',
                }}
              >
                <div className="label mono">
                  {name(r.node_id)} · {r.iface} → {r.target}
                </div>
                {line.ok ? (
                  <div className="unit">connected · {line.ms ?? 'time unknown'}</div>
                ) : (
                  <>
                    <Verbatim text={line.error ?? ''} size="unit" />
                    {line.ms ? <div className="unit">answered in {line.ms}</div> : null}
                  </>
                )}
              </div>
            )
          })}
        </div>
      ) : null}

      {view.findings.map((f, i) => (
        <div
          key={`${f.node_id}~${f.iface}~${i}`}
          style={{
            borderLeft: `var(--space-2) solid ${f.severity === 'warning' ? 'var(--warn)' : 'var(--rule)'}`,
            paddingLeft: 'var(--space-6)',
            marginTop: 'var(--space-10)',
          }}
        >
          <div className="label mono">
            {name(f.node_id)}
            {f.iface ? ` · ${f.iface}` : ''}
          </div>
          <Verbatim text={f.sentence} size="unit" />
          {f.fix ? <Copyable text={f.fix} /> : null}
        </div>
      ))}
    </div>
  )
}
