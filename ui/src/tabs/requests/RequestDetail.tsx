import type { MouseEvent, ReactNode } from 'react'
import type { RequestRow } from '../../api/types'
import { Icon } from '../../components/Icon'
import { Verbatim } from '../../components/Verbatim'
import { useKeyedResource } from '../../state/backend'
import { useCluster, useProviders } from '../../state/resources'
import { plainClick, useRouter, type Route } from '../../state/router'
import { count, money } from '../keys/rows'
import { cachedShare, servedBy } from './servedBy'

const dash = (v: string | number | null | undefined): string =>
  v == null || v === '' ? '—' : String(v)

/** `/usage/requests/<id>`: everything recorded about one client request, and each
 *  attempt the gateway made to answer it. */
export function RequestDetail({ requestId }: { requestId: string }) {
  const { navigate, linkTo } = useRouter()
  const providers = useProviders().data ?? []
  const deployments = useCluster().data?.deployments ?? []
  // 5 s, the coordinator's journal ship interval (telemetry/config.py
  // SHIP_INTERVAL_S): a link followed the moment a request finishes -- the
  // setup screen's test request, a chat turn -- arrives before the row does,
  // and should turn into the row on its own rather than after half a minute.
  const res = useKeyedResource(
    requestId,
    (b) => b.requests({ request_id: requestId, start: '-30d', page_size: 1 }),
    5_000,
  )

  const go = (e: MouseEvent, patch: Partial<Route>) => {
    if (!plainClick(e)) return
    e.preventDefault()
    navigate(patch)
  }
  const back = (
    <a
      href={linkTo({ dest: 'usage', sub: 'requests', request: null })}
      onClick={(e) => go(e, { dest: 'usage', sub: 'requests', request: null })}
    >
      <Icon name="chevron-left" size={12} decorative /> All requests
    </a>
  )

  if (res.error && !res.data) {
    return (
      <div className="card2">
        {back}
        <Verbatim text={res.error.message} />
      </div>
    )
  }
  if (!res.data) {
    return (
      <div className="card2" aria-busy="true">
        {back}
        <div className="unit" aria-live="polite">
          Reading…
        </div>
      </div>
    )
  }
  const row: RequestRow | undefined = res.data.rows[0]
  if (res.data.total === 0 || !row) {
    return (
      <div className="card2">
        {back}
        <p className="unit">
          No request {requestId} in the last 30 days. One that finished seconds ago may not be
          recorded yet; this page checks again every 5 s.
        </p>
      </div>
    )
  }

  const keyId = row.key_id
  const facts: [string, ReactNode][] = [
    ['Time', new Date(row.ts * 1000).toLocaleString()],
    [
      'Key',
      keyId ? (
        <a
          href={linkTo({ dest: 'usage', sub: 'keys', key: keyId })}
          onClick={(e) => go(e, { dest: 'usage', sub: 'keys', key: keyId })}
        >
          {row.key_name ?? keyId}
        </a>
      ) : (
        '—'
      ),
    ],
    [
      'Model',
      <span className="mono">
        {dash(row.model_requested)} → {dash(row.model_served ?? row.served_name)}
      </span>,
    ],
    [
      'Status',
      <>
        {dash(row.status)}
        {row.status === 429 ? <span style={{ color: 'var(--warn)' }}> · rate limited</span> : null}
        {row.error_class || row.error_code ? (
          <span className="mono unit"> {[row.error_class, row.error_code].filter(Boolean).join(' · ')}</span>
        ) : null}
      </>,
    ],
    ['Finish reason', dash(row.finish_reason)],
    ['Policy', dash(row.policy)],
    ['Served by', servedBy(row, providers, deployments)],
    ['Node', <span className="mono">{dash(row.node_id)}</span>],
    ['Client IP', <span className="mono">{dash(row.client_ip)}</span>],
    ['User agent', <span className="mono">{dash(row.user_agent)}</span>],
    ['Tokens in / out', `${count(row.prompt_tokens)} / ${count(row.completion_tokens)}`],
    [
      'Tokens cached (read / write)',
      <>
        {`${count(row.cache_read_tokens)} / ${count(row.cache_write_tokens)}`}
        {row.cache_read_tokens == null && row.cache_error ? (
          <span className="mono unit"> {row.cache_error}</span>
        ) : cachedShare(row.cache_read_tokens, row.prompt_tokens) ? (
          <span className="unit"> {cachedShare(row.cache_read_tokens, row.prompt_tokens)}</span>
        ) : null}
      </>,
    ],
    ['TTFT ms', count(row.ttft_ms)],
    ['Duration ms', count(row.duration_ms)],
    ['Decode ms', count(row.decode_ms)],
    ['Queue ms', count(row.parked_ms)],
    ['Tokens/s', count(row.tokens_per_s)],
    ['TPOT p95 ms', count(row.tpot_p95_ms)],
    ['Cost', money(row.cost_usd)],
  ]
  if (row.limit_source || row.admission_code) {
    facts.push(['Limit', [row.limit_source, row.admission_code].filter(Boolean).join(' · ')])
  }

  const chain: NonNullable<RequestRow['attempt_chain']> = row.attempt_chain ?? [
    {
      target_id: row.target_id,
      status: row.status,
      error_class: row.error_class || null,
      retry_reason: row.retry_reason || null,
      ms: row.duration_ms,
    },
  ]

  return (
    <div className="card2">
      {back}
      <h3 style={{ marginTop: 'var(--space-8)' }}>
        <span className="mono">{row.request_id}</span>
      </h3>
      <table>
        <tbody>
          {facts.map(([k, v]) => (
            <tr key={k}>
              <th scope="row">{k}</th>
              <td>{v}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4 className="label" style={{ marginTop: 'var(--space-12)' }}>
        Attempts
      </h4>
      <table>
        <thead>
          <tr>
            <th scope="col" style={{ textAlign: 'right' }}>
              #
            </th>
            <th scope="col">Target</th>
            <th scope="col">Status</th>
            <th scope="col">Reason</th>
            <th scope="col">Retry delay</th>
            <th scope="col" style={{ textAlign: 'right' }}>
              ms
            </th>
          </tr>
        </thead>
        <tbody>
          {chain.map((a, i) => (
            <tr key={i}>
              <td className="num">{i + 1}</td>
              <td className="mono">{dash(a.target_id)}</td>
              <td className="mono">{dash(a.status ?? a.error_class)}</td>
              <td style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                {a.error_message ?? dash(a.retry_reason)}
              </td>
              <td className="num">{a.retry_after_s != null ? `${Math.ceil(a.retry_after_s)} s` : '—'}</td>
              <td className="num">{count(a.ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
