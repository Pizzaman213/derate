import { useEffect, useState, type MouseEvent } from 'react'
import type { KeyFilter, KeyStatus } from '../../api/types'
import { Lamp } from '../../components/Lamp'
import { Verbatim } from '../../components/Verbatim'
import { useKeyedResource, useResource } from '../../state/backend'
import { plainClick, useRouter } from '../../state/router'
import { KeyCreate } from './KeyCreate'
import { KeyDetail } from './KeyDetail'
import { OrgsCard } from './OrgsCard'
import { tableRow } from './rows'

const STATUSES: KeyStatus[] = ['active', 'disabled', 'expired']
const EXPIRING = [1, 7, 30] as const

/** `/usage/keys` is the table; `/usage/keys/<id>` is one key's page. */
export function KeysSub() {
  const { route } = useRouter()
  if (route.key) return <KeyDetail keyId={route.key} />
  return <KeyList />
}

function KeyList() {
  const { navigate, linkTo } = useRouter()
  const [filter, setFilter] = useState<KeyFilter>({})
  const [qDraft, setQDraft] = useState('')

  // The search is the server's `q`; typing settles before it asks.
  useEffect(() => {
    const id = window.setTimeout(() => setFilter((f) => ({ ...f, q: qDraft.trim() || undefined })), 300)
    return () => window.clearTimeout(id)
  }, [qDraft])

  const keys = useKeyedResource(JSON.stringify(filter), (b) => b.keys(filter), 15_000)
  const models = useResource((b) => b.models(), 30_000)

  const now = Date.now() / 1000
  const rows = (keys.data ?? []).map((k) => tableRow(k, now))

  const open = (e: MouseEvent, keyId: string) => {
    if (!plainClick(e)) return
    e.preventDefault()
    navigate({ dest: 'usage', sub: 'keys', key: keyId })
  }

  return (
    <div>
      <div className="card2">
        <div
          role="search"
          style={{
            display: 'flex',
            gap: 'var(--space-10)',
            flexWrap: 'wrap',
            alignItems: 'center',
            margin: '0 0 var(--space-8)',
          }}
        >
          {/* Same shape as the audit log's row: the placeholder and first
              option say what each box filters, so the controls (and New key)
              share one centre line instead of hanging under labels. */}
          <input
            id="kf-q"
            type="search"
            aria-label="Search keys"
            placeholder="search keys"
            value={qDraft}
            onChange={(e) => setQDraft(e.target.value)}
          />
          <select
            id="kf-status"
            aria-label="Status"
            value={filter.status ?? ''}
            onChange={(e) =>
              setFilter((f) => ({
                ...f,
                status: (e.target.value || undefined) as KeyStatus | undefined,
              }))
            }
          >
            <option value="">any status</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <select
            id="kf-exp"
            aria-label="Expiring within"
            value={filter.expiring_days ?? ''}
            onChange={(e) =>
              setFilter((f) => ({
                ...f,
                expiring_days: e.target.value ? Number(e.target.value) : undefined,
              }))
            }
          >
            <option value="">expiring any time</option>
            {EXPIRING.map((d) => (
              <option key={d} value={d}>
                {d === 1 ? 'expiring within 1 day' : `expiring within ${d} days`}
              </option>
            ))}
          </select>
          <label className="sw">
            <input
              type="checkbox"
              checked={!!filter.near_limit}
              onChange={(e) =>
                setFilter((f) => ({
                  ...f,
                  near_limit: e.target.checked || undefined,
                }))
              }
            />
            <span>Near a limit</span>
          </label>
          {/* No heading: the nav already says Keys. New key takes the row's
              right end, the corner the heading used to give it. */}
          <span style={{ marginLeft: 'auto' }}>
            <KeyCreate models={models.data ?? []} />
          </span>
        </div>
        <div className="unit" style={{ marginBottom: 'var(--space-8)' }}>
          Usage columns cover the last 30 days.
        </div>

        {keys.error ? <Verbatim text={keys.error.message} /> : null}
        <div style={{ overflowX: 'auto' }} aria-busy={keys.loading}>
          <table>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Key id</th>
                <th scope="col">Status</th>
                <th scope="col">Tightest limit</th>
                <th scope="col">Expires</th>
                <th scope="col">Last used</th>
                <th scope="col" style={{ textAlign: 'right' }}>
                  Requests
                </th>
                <th scope="col" style={{ textAlign: 'right' }}>
                  Tokens
                </th>
                <th scope="col" style={{ textAlign: 'right' }}>
                  Cost
                </th>
                <th scope="col">Created by</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.keyId}>
                  <td>
                    <span
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 'var(--space-6)',
                      }}
                    >
                      <Lamp signal={r.lamp.signal} hollow={r.lamp.hollow} label={r.lamp.label} />
                      <a href={linkTo({ dest: 'usage', sub: 'keys', key: r.keyId })} onClick={(e) => open(e, r.keyId)}>
                        {r.name}
                      </a>
                      {r.admin ? <span className="pill">admin</span> : null}
                    </span>
                  </td>
                  <td className="mono">{r.keyId}</td>
                  <td className="unit">{r.deleted ? `${r.status}, deleted` : r.status}</td>
                  <td className="unit">{r.limitText ?? ''}</td>
                  <td className="unit" title={r.expiresAbs}>
                    {r.expires}
                  </td>
                  <td className="unit">{r.lastUsed}</td>
                  <td className="num">{r.usage.requests}</td>
                  <td className="num">{r.usage.tokens}</td>
                  <td className="num">{r.usage.cost}</td>
                  <td className="unit">{r.createdBy}</td>
                </tr>
              ))}
              {keys.data && rows.length === 0 ? (
                <tr>
                  <td colSpan={10} className="unit">
                    No keys match. Create one above.
                  </td>
                </tr>
              ) : null}
              {keys.loading ? (
                <tr>
                  <td colSpan={10} className="unit" aria-live="polite">
                    Reading…
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>
      <OrgsCard />
    </div>
  )
}
