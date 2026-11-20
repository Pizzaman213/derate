import { RequestRows } from '../requests/RequestRows'
import { useState } from 'react'
import type { UsageBucket, UsageResult } from '../../api/types'
import { Verbatim } from '../../components/Verbatim'
import { useBackend, useKeyedResource } from '../../state/backend'
import { Chart, ChartGrid } from '../dashboard/Chart'
import {
  breakdown,
  count,
  errorClasses,
  fieldChips,
  money,
  pctOf,
  series,
  type Breakdown,
} from './rows'

const WINDOWS = ['24h', '7d', '30d'] as const
type StatsWindow = (typeof WINDOWS)[number]
const BUCKET = '1h'
const PAGE_SIZE = 25

const DIMS: { dim: string; title: string }[] = [
  { dim: 'model', title: 'By model' },
  { dim: 'status', title: 'By status' },
  { dim: 'context_bucket', title: 'By context size' },
]

/** Thirty days (by default) of one key's traffic off the telemetry archive:
 *  the charts, why any measure is absent, three breakdowns, and the raw
 *  requests with a CSV of them. Every chart is `dashboard/Chart.tsx`, so a
 *  bucket nothing reported is a hole in the line rather than a zero. */
export function KeyStats({ keyId }: { keyId: string }) {
  const [win, setWin] = useState<StatsWindow>('30d')
  const start = `-${win}`
  const usage = useKeyedResource(
    `${keyId}|${win}`,
    (b) => b.usage({ key_id: keyId, start, bucket: BUCKET }),
    60_000,
  )

  return (
    <div className="card2">
      <h3 style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 'var(--space-8)' }}>
        <span>Usage</span>
        <span className="chips" role="group" aria-label="Usage window">
          {WINDOWS.map((w) => (
            <button type="button" key={w} aria-pressed={w === win} onClick={() => setWin(w)}>
              {w}
            </button>
          ))}
        </span>
      </h3>
      {usage.error ? <Verbatim text={usage.error.message} /> : null}
      {usage.data ? <UsageCharts result={usage.data} /> : usage.loading ? <div className="unit">Reading…</div> : null}

      {DIMS.map((d) => (
        <BreakdownTable key={d.dim} keyId={keyId} start={start} win={win} dim={d.dim} title={d.title} />
      ))}
      <RecentRequests key={win} keyId={keyId} start={start} win={win} />
    </div>
  )
}

/** One ungrouped `GET /api/usage` answer as charts, chips and error classes. */
function UsageCharts({ result }: { result: UsageResult }) {
  const rows = result.rows
  const s = (pick: (b: UsageBucket) => number | null | undefined) =>
    series(rows, pick, result.from, result.to, result.bucket)
  const pct = (pick: (b: UsageBucket) => number | null) =>
    s((b) => {
      const v = pick(b)
      return v == null ? null : v * 100
    })
  const requests = s((b) => b.requests)
  const note = `${requests.length} × ${result.bucket}`
  const chips = fieldChips(rows)
  const classes = errorClasses(rows)

  return (
    <div>
      {chips.length ? (
        <div style={{ display: 'flex', gap: 'var(--space-6)', flexWrap: 'wrap', marginBottom: 'var(--space-10)' }}>
          {chips.map((c) => (
            <span className="pill" key={`${c.field}:${c.code}`} title={c.field}>
              {c.field} <span className="mono">{c.text}</span>
            </span>
          ))}
        </div>
      ) : null}
      <ChartGrid>
        <Chart title="Requests" unit="reqs" points={requests} note={note} />
        <Chart title="Errors" unit="reqs" points={s((b) => b.failed)} note={note} />
        <Chart title="Tokens in" unit="tok" points={s((b) => b.tokens_in)} note={note} />
        <Chart title="Tokens out" unit="tok" points={s((b) => b.tokens_out)} note={note} />
        <Chart title="Cost" unit="$" decimals={4} points={s((b) => b.cost_usd)} note={note} />
        <Chart
          title="Time to first token"
          unit="ms"
          points={s(pctOf((b) => b.ttft_ms, 'p50'))}
          band={s(pctOf((b) => b.ttft_ms, 'p95'))}
          lowLabel="p50"
          bandLabel="p95"
          note={note}
        />
        <Chart
          title="Latency"
          unit="ms"
          points={s(pctOf((b) => b.latency_ms, 'p50'))}
          band={s(pctOf((b) => b.latency_ms, 'p95'))}
          lowLabel="p50"
          bandLabel="p95"
          note={note}
        />
        <Chart title="Tokens/s (p50)" unit="tok/s" decimals={1} points={s(pctOf((b) => b.tokens_per_s, 'p50'))} note={note} />
        <Chart title="Cache hit rate" unit="%" decimals={1} points={pct((b) => b.cache_hit_rate)} note={note} />
        <Chart
          title="Spec-decode acceptance"
          unit="%"
          decimals={1}
          points={pct((b) => b.spec_acceptance_rate)}
          note={note}
        />
      </ChartGrid>
      <h4 className="label" style={{ marginTop: 'var(--space-12)' }}>
        Errors by class
      </h4>
      {classes.length === 0 ? (
        <div className="unit">No failed requests in this window.</div>
      ) : (
        <table>
          <tbody>
            {classes.map(([cls, n]) => (
              <tr key={cls}>
                <td className="mono">{cls}</td>
                <td className="num">{count(n)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function BreakdownTable({
  keyId,
  start,
  win,
  dim,
  title,
}: {
  keyId: string
  start: string
  win: string
  dim: string
  title: string
}) {
  const res = useKeyedResource(
    `${keyId}|${win}|${dim}`,
    (b) => b.usage({ key_id: keyId, start, bucket: '1d', group_by: [dim] }),
    60_000,
  )
  const rows: Breakdown[] = res.data ? breakdown(res.data.rows, dim) : []
  return (
    <div style={{ marginTop: 'var(--space-12)' }}>
      <h4 className="label">{title}</h4>
      {res.error ? <Verbatim text={res.error.message} size="unit" /> : null}
      {res.data && rows.length === 0 ? <div className="unit">No requests in this window.</div> : null}
      {rows.length ? (
        <table>
          <thead>
            <tr>
              <th scope="col">{dim}</th>
              <th scope="col" style={{ textAlign: 'right' }}>
                Requests
              </th>
              <th scope="col" style={{ textAlign: 'right' }}>
                Failed
              </th>
              <th scope="col" style={{ textAlign: 'right' }}>
                Tokens in
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
            {rows.map((r) => (
              <tr key={r.value}>
                <td className="mono">{r.value}</td>
                <td className="num">{count(r.requests)}</td>
                <td className="num">{count(r.failed)}</td>
                <td className="num">{count(r.tokensIn)}</td>
                <td className="num">{count(r.tokensOut)}</td>
                <td className="num">{money(r.cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  )
}

function RecentRequests({ keyId, start, win }: { keyId: string; start: string; win: string }) {
  const { backend } = useBackend()
  const [page, setPage] = useState(1)
  const res = useKeyedResource(
    `${keyId}|${win}|${page}`,
    (b) => b.keyRequests(keyId, { start, page, page_size: PAGE_SIZE }),
    30_000,
  )
  const data = res.data
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1

  return (
    <div style={{ marginTop: 'var(--space-12)' }}>
      <h4 className="label" style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--space-8)' }}>
        <span>Recent requests</span>
        <a href={backend.keyRequestsCsvUrl(keyId, { start })} download>
          CSV
        </a>
      </h4>
      {res.error ? <Verbatim text={res.error.message} size="unit" /> : null}
      {data ? (
        <>
          <RequestRows rows={data.rows} showKey={false} />
          {pages > 1 ? (
            <div style={{ display: 'flex', gap: 'var(--space-8)', alignItems: 'center', marginTop: 'var(--space-8)' }}>
              <button type="button" className="ghost" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                Newer
              </button>
              <span className="unit">
                page {page} of {pages} · {count(data.total)} requests
              </span>
              <button type="button" className="ghost" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>
                Older
              </button>
            </div>
          ) : null}
        </>
      ) : res.loading ? (
        <div className="unit">Reading…</div>
      ) : null}
    </div>
  )
}
