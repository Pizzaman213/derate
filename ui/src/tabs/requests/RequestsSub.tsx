import { useState, type FormEvent } from 'react'
import type { RequestsQuery } from '../../api/types'
import { RequestMap } from '../../components/RequestMap'
import { Verbatim } from '../../components/Verbatim'
import { useBackend, useKeyedResource, useResource } from '../../state/backend'
import { useModels, useProviders } from '../../state/resources'
import { useRouter, type Route } from '../../state/router'
import { count } from '../keys/rows'
import { RequestDetail } from './RequestDetail'
import { RequestRows } from './RequestRows'
import { MAP_ATTRIBUTION, databaseSentence, originsSummary } from './origins'
import { windowStart } from './servedBy'

const WINDOWS = ['24h', '7d', '30d'] as const
const PAGE_SIZE = 100

const filtersForRequest = (route: Route, win: string) =>
  [win, route.fkey, route.fmodel, route.fstatus, route.ftarget, route.fprovider, route.fretried]

/** `/usage/requests`: one row per client request, cluster-wide.
 *  `/usage/requests/<id>` is that request's detail. Filters live in the URL. */
export function RequestsSub() {
  const { route } = useRouter()
  if (route.request) return <RequestDetail requestId={route.request} />
  return <RequestList />
}

function RequestList() {
  const { route, navigate } = useRouter()
  const keys = useResource((b) => b.keys(), 60_000)
  const models = useModels()
  const providers = useProviders()
  const [search, setSearch] = useState('')
  const set = (patch: Partial<Route>) => navigate(patch, { replace: true })
  const win = route.win === '7d' || route.win === '30d' ? route.win : '24h'

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const id = search.trim()
    if (id) navigate({ dest: 'usage', sub: 'requests', request: id })
  }

  const filters = filtersForRequest(route, win)
  return (
    <div className="card2">
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-8)', alignItems: 'center' }}>
        <label>
          Key{' '}
          <select value={route.fkey ?? ''} onChange={(e) => set({ fkey: e.target.value || null })}>
            <option value="">all</option>
            {(keys.data ?? []).map((k) => (
              <option key={k.key_id} value={k.key_id}>
                {k.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Model{' '}
          <select value={route.fmodel ?? ''} onChange={(e) => set({ fmodel: e.target.value || null, ftarget: null })}>
            <option value="">all</option>
            {route.fmodel && !(models.data ?? []).some((m) => m.id === route.fmodel)
              ? <option value={route.fmodel}>{route.fmodel}</option> : null}
            {(models.data ?? []).map((m) => (
              <option key={m.id} value={m.id}>
                {m.id}
              </option>
            ))}
          </select>
        </label>
        <label>
          Provider{' '}
          <select value={route.fprovider ?? ''} onChange={(e) => set({ fprovider: e.target.value || null, ftarget: null })}>
            <option value="">all</option>
            {route.fprovider && !(providers.data ?? []).some((p) => p.provider_id === route.fprovider)
              ? <option value={route.fprovider}>{route.fprovider}</option> : null}
            {(providers.data ?? []).map((p) => <option key={p.provider_id} value={p.provider_id}>{p.display_name}</option>)}
          </select>
        </label>
        <label>
          Status{' '}
          <select
            value={route.fstatus ?? ''}
            onChange={(e) => {
              const v = e.target.value
              set({ fstatus: v === 'ok' || v === 'failed' || v === '429' ? v : null })
            }}
          >
            <option value="">all</option>
            <option value="ok">ok</option>
            <option value="failed">failed</option>
            <option value="429">rate limited (429)</option>
          </select>
        </label>
        <label>
          <input type="checkbox" checked={route.fretried} onChange={(e) => set({ fretried: e.target.checked })} />{' '}
          failed over only
        </label>
        <form onSubmit={submit} style={{ display: 'flex', gap: 'var(--space-4)' }}>
          <input
            type="search"
            className="mono"
            aria-label="Request id"
            placeholder="request id (r-…)"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button type="submit" className="ghost">
            Open
          </button>
        </form>
        {/* The window sits in the card's top-right corner, like the audit log's.
            `.chips` keeps a bottom margin for its usual home above a grid; in a
            centred row that margin hangs the buttons above the selects. */}
        <span className="chips" role="group" aria-label="Window" style={{ margin: 0, marginLeft: 'auto' }}>
          {WINDOWS.map((w) => (
            <button type="button" key={w} aria-pressed={w === win} onClick={() => set({ win: w })}>
              {w}
            </button>
          ))}
        </span>
      </div>
      {route.ftarget ? <div className="unit" style={{ marginTop: 'var(--space-8)' }}>
        Target: <span className="mono">{route.ftarget}</span>{' '}
        <button type="button" className="ghost" onClick={() => set({ ftarget: null })}>All targets</button>
      </div> : null}
      <OriginsCard key={JSON.stringify(['origins', ...filters])} route={route} win={win} />
      <RequestPages key={JSON.stringify(filters)} route={route} win={win} />
    </div>
  )
}

/** The filters as `/api/requests` and `/api/requests/origins` both take them:
 *  one builder, so the map and the table can never mean different requests. */
function requestQuery(route: Route, win: string): RequestsQuery {
  return {
    start: windowStart(win),
    key_id: route.fkey ?? undefined,
    model: route.fmodel ?? undefined,
    status: route.fstatus === '429' ? 429 : route.fstatus ?? undefined,
    target_id: route.ftarget ?? undefined,
    provider_id: route.fprovider ?? undefined,
    retried: route.fretried || undefined,
  }
}

/** Where this window's requests came from, under the same filters as the list. */
function OriginsCard({ route, win }: { route: Route; win: string }) {
  const q = requestQuery(route, win)
  const res = useKeyedResource(
    JSON.stringify(['origins', ...filtersForRequest(route, win)]),
    (b) => b.requestOrigins(q),
    60_000,
  )
  const data = res.data
  return (
    <div style={{ marginTop: 'var(--space-12)' }}>
      {res.error ? <Verbatim text={res.error.message} size="unit" /> : null}
      {data ? (
        <>
          <div className="unit">
            {originsSummary(data, data.points.length)} {databaseSentence(data.database)}
            {/* The dots a reader is missing are almost always these: say how to draw them. */}
            {data.local ? ' A local-network request has no dot until its address is pinned: open one and pin it on its map.' : ''}
          </div>
          <RequestMap title="Where requests came from" height={320} points={data.points} attribution={MAP_ATTRIBUTION} />
        </>
      ) : res.loading ? (
        <div className="unit">Reading…</div>
      ) : null}
    </div>
  )
}

function RequestPages({ route, win }: { route: Route; win: string }) {
  const { backend } = useBackend()
  const [page, setPage] = useState(1)
  const q = requestQuery(route, win)
  const res = useKeyedResource(
    JSON.stringify([...filtersForRequest(route, win), page]),
    (b) => b.requests({ ...q, page, page_size: PAGE_SIZE }),
    15_000,
  )
  const data = res.data
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1

  return (
    <div style={{ marginTop: 'var(--space-12)' }}>
      <h4 className="label" style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--space-8)' }}>
        <span>{data ? `${count(data.total)} ${data.total === 1 ? 'request' : 'requests'}` : 'Requests'}</span>
        <a href={backend.requestsCsvUrl(q)} download>
          CSV
        </a>
      </h4>
      {res.error ? <Verbatim text={res.error.message} size="unit" /> : null}
      {data ? (
        <>
          <RequestRows rows={data.rows} showKey />
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
