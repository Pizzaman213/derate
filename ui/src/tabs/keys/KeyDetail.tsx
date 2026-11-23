import type { MouseEvent } from 'react'
import type { LimitState } from '../../api/types'
import { ProportionBar } from '../../components/Bars'
import { Icon } from '../../components/Icon'
import { Lamp } from '../../components/Lamp'
import { Verbatim } from '../../components/Verbatim'
import { useKeyedResource } from '../../state/backend'
import { plainClick, useRouter } from '../../state/router'
import { KeyActions } from './KeyActions'
import { KeyStats } from './KeyStats'
import {
  absTime,
  expiresText,
  lampFor,
  lastUsedText,
  limitPhase,
  limitValue,
  percent,
  raised,
  untilText,
} from './rows'

/** `/usage/keys/<id>`: who the key is, where it stands against every limit, what
 *  can be done to it, and thirty days of what it did. A deleted or expired
 *  key still reads -- its history is the reason to open it. */
export function KeyDetail({ keyId }: { keyId: string }) {
  const { navigate, linkTo } = useRouter()
  const key = useKeyedResource(keyId, (b) => b.key(keyId), 10_000)

  const go = (e: MouseEvent, patch: Parameters<typeof navigate>[0]) => {
    if (!plainClick(e)) return
    e.preventDefault()
    navigate(patch)
  }

  const back = (
    <a href={linkTo({ dest: 'usage', sub: 'keys', key: null })} onClick={(e) => go(e, { dest: 'usage', sub: 'keys', key: null })}>
      <Icon name="chevron-left" size={12} decorative /> All keys
    </a>
  )

  if (key.error && !key.data) {
    return (
      <div className="card2">
        {back}
        <Verbatim text={key.error.message} />
      </div>
    )
  }
  const row = key.data
  if (!row) {
    return (
      <div className="card2" aria-busy="true">
        {back}
        <div className="unit" aria-live="polite">
          Reading…
        </div>
      </div>
    )
  }

  const now = Date.now() / 1000
  const lamp = lampFor(row)
  const states = row.limit_states ?? []

  return (
    <div>
      <div className="card2">
        {back}
        <h3 style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-8)', marginTop: 'var(--space-8)' }}>
          <Lamp signal={lamp.signal} hollow={lamp.hollow} label={lamp.label} />
          <span>{row.name}</span>
          <span className="mono unit">{row.key_id}</span>
          {row.is_admin_key ? <span className="pill">admin key · {row.admin_username ?? ''}</span> : null}
        </h3>
        <table>
          <tbody>
            <tr>
              <th scope="row">Status</th>
              <td>{row.deleted_at != null ? `${row.status}, deleted ${untilText(row.deleted_at, now)}` : row.status}</td>
            </tr>
            {row.status === 'disabled' ? (
              <tr>
                <th scope="row">Disabled</th>
                <td>
                  {absTime(row.disabled_at)} by {row.disabled_by ?? '—'}
                  {row.disabled_reason ? ` — ${row.disabled_reason}` : ''}
                </td>
              </tr>
            ) : null}
            <tr>
              <th scope="row">Expires</th>
              <td title={absTime(row.expires_at)}>
                {expiresText(row, now)}
                {row.expires_at != null ? <span className="unit"> · {absTime(row.expires_at)}</span> : null}
              </td>
            </tr>
            <tr>
              <th scope="row">Created</th>
              <td>
                {absTime(row.created_at)} by {row.created_by}
              </td>
            </tr>
            <tr>
              <th scope="row">Last used</th>
              <td>{lastUsedText(row, now)}</td>
            </tr>
            <tr>
              <th scope="row">Models</th>
              <td className="mono">{row.allowed_models == null ? 'every served model' : row.allowed_models.join(', ') || 'none'}</td>
            </tr>
            <tr>
              <th scope="row">Routing</th>
              <td className="mono">{row.routing_policy ?? 'inherit'}</td>
            </tr>
            <tr>
              <th scope="row">Cloud fallback</th>
              <td className="mono">{row.cloud_fallback_local_model ?? 'none'}</td>
            </tr>
            {row.notes ? (
              <tr>
                <th scope="row">Notes</th>
                <td style={{ whiteSpace: 'pre-wrap' }}>{row.notes}</td>
              </tr>
            ) : null}
          </tbody>
        </table>
        <div style={{ marginTop: 'var(--space-12)' }}>
          <KeyActions row={row} />
        </div>
      </div>

      <div className="card2">
        <h3>Limits this cycle</h3>
        {states.length === 0 ? (
          <div className="unit">No limits apply to this key.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th scope="col">Limit</th>
                <th scope="col">Scope</th>
                <th scope="col" style={{ textAlign: 'right' }}>
                  Used
                </th>
                <th scope="col" style={{ textAlign: 'right' }}>
                  Limit
                </th>
                <th scope="col">Share</th>
                <th scope="col">Resets</th>
              </tr>
            </thead>
            <tbody>
              {states.map((s) => (
                <LimitRow key={`${s.scope}:${s.limit_type}`} s={s} now={now} />
              ))}
            </tbody>
          </table>
        )}
      </div>

      <KeyStats keyId={row.key_id} />
    </div>
  )
}

function LimitRow({ s, now }: { s: LimitState; now: number }) {
  const phase = limitPhase(s.fraction)
  const tone = phase === 'ok' ? 'ink' : 'warn'
  return (
    <tr>
      <td className="mono">{s.limit_type}</td>
      <td className="unit">{s.scope}</td>
      <td className="num">{limitValue(s.limit_type, s.used)}</td>
      <td className="num">
        {limitValue(s.limit_type, s.effective_limit)}
        {raised(s) ? (
          <div className="unit">raised this cycle, configured {limitValue(s.limit_type, s.limit)}</div>
        ) : null}
      </td>
      <td>
        {/* baseline, not center: the % label is a smaller font than the row,
            and centring it against the bar pulls it off the row's baseline. */}
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--space-6)' }}>
          <ProportionBar
            value={s.fraction}
            width="var(--space-96)"
            tone={tone}
            label={`${s.limit_type} ${percent(s.fraction)}% used`}
          />
          <span className="mono unit">{percent(s.fraction)}%</span>
        </div>
      </td>
      <td className="unit" title={s.resets_at == null ? undefined : absTime(s.resets_at)}>
        {/* An empty rpm/tpm window reports resets_at = now: the whole budget
            is available already, which "1s ago" would misstate. */}
        {s.resets_at == null ? 'per request' : s.resets_at <= now ? 'now' : untilText(s.resets_at, now)}
      </td>
    </tr>
  )
}
