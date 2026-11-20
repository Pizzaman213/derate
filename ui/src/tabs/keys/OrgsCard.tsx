import { useState } from 'react'
import type { ApiKeyRow, OrgRow } from '../../api/types'
import { Verbatim } from '../../components/Verbatim'
import { useBackend, useResource } from '../../state/backend'

/** One row per org, with the containment step for it: disable every active
 *  key at once (`POST /api/orgs/<id>/keys/disable`), admin keys included.
 *  Behind a confirm that says so; the server writes one `key_disabled` row
 *  per key with the reason, and open realtime sessions on those keys close
 *  within seconds. Each key is re-enabled one at a time, on its own page. */
export function OrgsCard() {
  const orgs = useResource<OrgRow[]>((b) => b.orgs(), 30_000)
  const keys = useResource<ApiKeyRow[]>((b) => b.keys(), 30_000)
  const { backend, invalidate } = useBackend()

  const [pending, setPending] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const active = new Map<string, number>()
  for (const k of keys.data ?? []) {
    if (k.status === 'active' && k.deleted_at == null) active.set(k.org_id, (active.get(k.org_id) ?? 0) + 1)
  }

  const run = async (org: OrgRow) => {
    setBusy(true)
    setError(null)
    try {
      const res = await backend.disableOrgKeys(org.org_id, reason.trim() || undefined)
      setDone(`Disabled ${res.disabled.length} key${res.disabled.length === 1 ? '' : 's'} in ${org.name}.`)
      setPending(null)
      setReason('')
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const rows = orgs.data ?? []
  const open = rows.find((o) => o.org_id === pending) ?? null

  return (
    <div className="card2">
      <h3>Organisations</h3>
      {orgs.error ? <Verbatim text={orgs.error.message} size="unit" /> : null}
      {!orgs.data ? (
        orgs.error ? null : (
          <div className="unit" aria-busy="true" aria-live="polite">
            Reading organisations…
          </div>
        )
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Org id</th>
                <th scope="col" style={{ textAlign: 'right' }}>
                  Active keys
                </th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((o) => (
                <tr key={o.org_id}>
                  <td>
                    <span style={{ display: 'inline-flex', gap: 'var(--space-6)', alignItems: 'baseline' }}>
                      <span>{o.name}</span>
                      {o.is_default ? <span className="pill">default</span> : null}
                    </span>
                  </td>
                  <td className="mono">{o.org_id}</td>
                  <td className="num">{keys.data ? (active.get(o.org_id) ?? 0) : ''}</td>
                  <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                    <button
                      type="button"
                      className={pending === o.org_id ? undefined : 'ghost'}
                      aria-expanded={pending === o.org_id}
                      disabled={keys.data != null && !active.get(o.org_id)}
                      onClick={() => {
                        setPending(pending === o.org_id ? null : o.org_id)
                        setError(null)
                        setDone(null)
                      }}
                    >
                      Disable every key
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open ? (
        <div className="card2" style={{ marginTop: 'var(--space-8)' }}>
          <div className="unit">
            Every active key in {open.name}, admin keys included, is refused with api_key_disabled from the next
            request, and open realtime sessions on them close. Each key is enabled again on its own page.
          </div>
          <div className="fld" style={{ marginTop: 'var(--space-8)' }}>
            <label htmlFor="oc-reason">Reason (optional)</label>
            <input id="oc-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
          </div>
          <div style={{ display: 'flex', gap: 'var(--space-8)', marginTop: 'var(--space-8)' }}>
            <button type="button" disabled={busy} onClick={() => void run(open)}>
              {busy ? 'Disabling…' : `Disable every key in ${open.name}`}
            </button>
            <button type="button" className="ghost" disabled={busy} onClick={() => setPending(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      {error ? <Verbatim text={error} size="unit" /> : null}
      {done ? (
        <div className="unit" role="status">
          {done}
        </div>
      ) : null}
    </div>
  )
}
