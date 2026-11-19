import type { MouseEvent } from 'react'
import type { RequestRow } from '../../api/types'
import { useCluster, useProviders } from '../../state/resources'
import { plainClick, useRouter } from '../../state/router'
import { count, money } from '../keys/rows'
import { cachedShare, servedBy } from './servedBy'

/** The request table: one row per client request, each a link to its detail.
 *  Shared by the Requests screen and a key's Recent requests. */
export function RequestRows({ rows, showKey }: { rows: RequestRow[]; showKey: boolean }) {
  const { navigate, linkTo } = useRouter()
  const providers = useProviders().data ?? []
  const deployments = useCluster().data?.deployments ?? []

  const open = (e: MouseEvent, requestId: string) => {
    if (!plainClick(e)) return
    e.preventDefault()
    navigate({ dest: 'usage', sub: 'requests', request: requestId })
  }

  const cols = showKey ? 11 : 10
  return (
    <div style={{ overflowX: 'auto' }}>
      <table>
        <thead>
          <tr>
            <th scope="col">Time</th>
            {showKey ? <th scope="col">Key</th> : null}
            <th scope="col">Model</th>
            <th scope="col">Served by</th>
            <th scope="col" style={{ textAlign: 'right' }}>
              Status
            </th>
            <th scope="col" style={{ textAlign: 'right' }}>
              Attempts
            </th>
            <th scope="col" style={{ textAlign: 'right' }}>
              TTFT ms
            </th>
            <th scope="col" style={{ textAlign: 'right' }}>
              Duration ms
            </th>
            <th scope="col" style={{ textAlign: 'right' }}>
              Tokens cached
            </th>
            <th scope="col" style={{ textAlign: 'right' }}>
              Tokens out
            </th>
            <th scope="col" style={{ textAlign: 'right' }}>
              Cost
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const keyId = r.key_id
            return (
            <tr key={r.request_id}>
              <td className="unit">
                <a
                  href={linkTo({ dest: 'usage', sub: 'requests', request: r.request_id })}
                  onClick={(e) => open(e, r.request_id)}
                >
                  {new Date(r.ts * 1000).toLocaleString()}
                </a>
              </td>
              {showKey ? (
                <td>
                  {keyId ? (
                    <a
                      href={linkTo({ dest: 'usage', sub: 'keys', key: keyId })}
                      onClick={(e) => {
                        e.stopPropagation()
                        if (!plainClick(e)) return
                        e.preventDefault()
                        navigate({ dest: 'usage', sub: 'keys', key: keyId })
                      }}
                    >
                      {r.key_name ?? keyId}
                    </a>
                  ) : (
                    '—'
                  )}
                </td>
              ) : null}
              <td className="mono">{r.served_name}</td>
              <td>{servedBy(r, providers, deployments)}</td>
              <td className="num">
                {r.status ?? '—'}
                {r.status === 429 ? <div className="unit" style={{ color: 'var(--warn)' }}>rate limited</div> : null}
                {r.error_class ? <div className="unit mono">{r.error_class}</div> : null}
              </td>
              <td className="num">{r.attempts > 1 ? `${r.attempts} attempts` : '1'}</td>
              <td className="num">{count(r.ttft_ms)}</td>
              <td className="num">{count(r.duration_ms)}</td>
              <td className="num">
                {count(r.cache_read_tokens)}
                {r.cache_read_tokens == null && r.cache_error ? (
                  <div className="unit mono">{r.cache_error}</div>
                ) : cachedShare(r.cache_read_tokens, r.prompt_tokens) ? (
                  <div className="unit">{cachedShare(r.cache_read_tokens, r.prompt_tokens)}</div>
                ) : null}
              </td>
              <td className="num">{count(r.completion_tokens)}</td>
              <td className="num">{money(r.cost_usd)}</td>
            </tr>
            )
          })}
          {rows.length === 0 ? (
            <tr>
              <td colSpan={cols} className="unit">
                No requests in this window.
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  )
}
