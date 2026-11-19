import { useState } from 'react'
import type { RequestsQuery } from '../../api/types'
import { PackChart } from '../../components/PackChart'
import { Verbatim } from '../../components/Verbatim'
import { useBackend, useKeyedResource } from '../../state/backend'
import { useCluster, useModels, useProviders } from '../../state/resources'
import { plainClick, useRouter } from '../../state/router'
import { REQUEST_WINDOWS } from '../../state/routes'
import { breakdown } from '../keys/rows'
import { rateLimitAttempt } from './servedBy'

/** Current admission and durable request failures are separate facts. A ready
 *  model must not erase yesterday's 429, nor turn a historical 503 into one. */
export function RateLimitsSub() {
  const { route, navigate, linkTo } = useRouter()
  const { backend } = useBackend()
  const providers = useProviders()
  const cluster = useCluster()
  const models = useModels()
  const deployment = (cluster.data?.deployments ?? []).find((d) => d.deployment_id === route.dep)
  const model = route.fmodel ?? deployment?.served_name ?? route.dep
  const win = route.win === '24h' || route.win === '7d' ? route.win : '30d'
  const query: RequestsQuery = {
    start: `-${win}`, model: route.ftarget ? undefined : model ?? undefined,
    provider_id: route.fprovider ?? undefined, target_id: route.ftarget ?? undefined,
  }
  const identity = JSON.stringify(query)
  const [paging, setPaging] = useState({ identity, page: 1 })
  const page = paging.identity === identity ? paging.page : 1
  const failures = useKeyedResource(`rate-failures|${identity}`, (b) =>
    b.requests({ ...query, status: 'failed', page_size: 1 }), 15_000)
  const limits = useKeyedResource(`rate-log|${identity}|${page}`, (b) => b.requests({ ...query, status: 429, page, page_size: 50 }), 15_000)
  const modelUsage = useKeyedResource(`rate-model-usage|${win}`, (b) => b.usage({
    start: `-${win}`, bucket: win === '24h' ? '1h' : '1d', group_by: ['model', 'provider'],
  }), 15_000)
  const modelErrors = modelUsage.data ? breakdown(modelUsage.data.rows.filter((row) =>
    !route.fprovider || row.group.provider === route.fprovider), 'model')
    .filter((row) => row.failed > 0)
    .map((row) => ({ label: row.value, value: row.failed })) : null
  const current = (providers.data ?? []).flatMap((p) => p.models.map((m) => ({ provider: p, model: m })))
    .filter(({ provider: p, model: m }) => (!model || m.served_name === model)
      && (!route.fprovider || route.fprovider === p.provider_id)
      && (!route.ftarget || route.ftarget === `${p.provider_id}:${m.upstream_id}`))
  const modelNames = [...new Set([
    ...(models.data ?? []).map((m) => m.id),
    ...(providers.data ?? []).flatMap((p) => p.models.map((m) => m.served_name)),
    ...(model ? [model] : []),
  ])].sort()
  const failedTotal = failures.data?.total ?? null
  const limited = limits.data?.total ?? null
  const pages = limits.data ? Math.max(1, Math.ceil(limits.data.total / 50)) : 1
  const set = (patch: Parameters<typeof navigate>[0]) => navigate(patch, { replace: true })

  return <div className="card2">
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-8)', alignItems: 'center', marginBottom: 'var(--space-12)' }}>
      <label>Model{' '}<select value={model ?? ''} onChange={(e) => set({ dep: e.target.value || null, fmodel: null, ftarget: null })}>
        <option value="">all</option>
        {modelNames.map((name) => <option key={name} value={name}>{name}</option>)}
      </select></label>
      <label>Provider{' '}<select value={route.fprovider ?? ''} onChange={(e) => set({ fprovider: e.target.value || null, ftarget: null })}>
        <option value="">all</option>
        {route.fprovider && !(providers.data ?? []).some((p) => p.provider_id === route.fprovider)
          ? <option value={route.fprovider}>{route.fprovider}</option> : null}
        {(providers.data ?? []).map((p) => <option key={p.provider_id} value={p.provider_id}>{p.display_name}</option>)}
      </select></label>
      <span className="chips" role="group" aria-label="Rate limit window" style={{ margin: 0, marginLeft: 'auto' }}>
        {REQUEST_WINDOWS.map((w) => <button key={w} type="button" aria-pressed={w === win} onClick={() => set({ win: w })}>{w}</button>)}
      </span>
    </div>
    {route.ftarget ? <div className="unit" style={{ marginTop: 'var(--space-8)' }}>
      Target: <span className="mono">{route.ftarget}</span>{' '}
      <button type="button" className="ghost" onClick={() => set({ ftarget: null })}>All targets</button>
    </div> : null}
    <div className="chartgrid">
      <div style={{ minWidth: 0 }}>
        <div className="unit">Selected model/target · errors only</div>
        {failedTotal != null && limited != null ? <PackChart
          type="donut" title="Request errors" height={240}
          data={[
            { label: 'Rate limited', value: limited },
            { label: 'Other errors', value: Math.max(0, failedTotal - limited) },
          ]}
          options={{ labels: 'auto', valueFormat: 'integer', centerLabel: 'Errors' }}
        /> : null}
      </div>
      <div style={{ minWidth: 0 }}>
        <div className="unit">All models · errors only</div>
        {modelErrors ? <PackChart
          type="donut" title="Errors by model" height={240} data={modelErrors}
          options={{ legend: false, labels: 'auto', valueFormat: 'integer', centerLabel: 'Errors' }}
        /> : modelUsage.error ? null : 'Loading model error statistics…'}
        {modelUsage.error ? <Verbatim text={modelUsage.error.message} size="unit" /> : null}
      </div>
    </div>
    {failures.error ? <Verbatim text={failures.error.message} size="unit" /> : null}
    {providers.error ? <Verbatim text={providers.error.message} size="unit" /> : null}
    <h4 className="label">Current model state</h4>
    <div style={{ overflowX: 'auto' }}><table aria-label="Current model rate limits">
      <thead><tr><th>Provider</th><th>Model</th><th>State now</th><th>Upstream error</th></tr></thead>
      <tbody>{current.map(({ provider: p, model: m }) => <tr key={`${p.provider_id}:${m.upstream_id}`}>
        <td>{p.display_name}</td><td className="mono">{m.served_name}</td>
        <td style={{ color: m.last_error ? 'var(--warn)' : 'var(--ink-muted)' }}>{m.admission_block ?? (m.admitting == null ? 'state unknown' : m.admitting ? 'admitting' : 'not admitting')}</td>
        <td style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{m.last_error ?? '—'}</td>
      </tr>)}{current.length === 0 ? <tr><td colSpan={4} className="unit">{providers.loading ? 'Reading model state…' : 'No currently configured target for this selection. Historical records remain below.'}</td></tr> : null}</tbody>
    </table></div>
    <h4 className="label" style={{ marginTop: 'var(--space-12)', display: 'flex', justifyContent: 'space-between' }}>
      <span>Recorded rate-limit log</span><a href={backend.requestsCsvUrl({ ...query, status: 429 })} download>CSV</a>
    </h4>
    {limits.error ? <Verbatim text={limits.error.message} size="unit" /> : null}
    <div style={{ overflowX: 'auto' }}><table aria-label="Recorded rate limits">
      <thead><tr><th>Time / request</th><th>Provider</th><th>Model</th><th>Retry delay</th><th>Recorded upstream error</th></tr></thead>
      <tbody>{(limits.data?.rows ?? []).map((r) => {
        const attempt = rateLimitAttempt(r)
        const patch = { dest: 'usage', sub: 'requests', request: r.request_id, dep: null } as const
        return <tr key={r.request_id}>
          <td><a href={linkTo(patch)} onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(patch) } }}>
            {new Date(r.ts * 1000).toLocaleString()}<div className="mono unit">{r.request_id}</div>
          </a></td>
          <td>{(providers.data ?? []).find((p) => p.provider_id === r.provider_id)?.display_name ?? r.provider_id}</td>
          <td className="mono">{r.served_name}</td>
          <td className="num">{attempt?.retry_after_s != null ? `${Math.ceil(attempt.retry_after_s)} s` : '—'}</td>
          <td style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{attempt?.error_message ?? 'Not recorded for this request.'}</td>
        </tr>
      })}{limits.data?.total === 0 ? <tr><td colSpan={5} className="unit">No recorded 429s for this selection in the last {win}.</td></tr> : null}</tbody>
    </table></div>
    {!limits.data && limits.loading ? <div className="unit">Reading recorded rate limits…</div> : null}
    {pages > 1 ? <div style={{ display: 'flex', gap: 'var(--space-8)', alignItems: 'center', marginTop: 'var(--space-8)' }}>
      <button type="button" disabled={page <= 1} onClick={() => setPaging({ identity, page: page - 1 })}>Newer</button>
      <span className="unit">page {page} of {pages}</span>
      <button type="button" disabled={page >= pages} onClick={() => setPaging({ identity, page: page + 1 })}>Older</button>
    </div> : null}
  </div>
}
