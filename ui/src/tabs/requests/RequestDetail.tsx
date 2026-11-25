import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import type { GeoLookup, RequestRoute, RequestRow, RouteHop } from '../../api/types'
import { ProportionBar } from '../../components/Bars'
import { copyToClipboard } from '../../components/clipboard'
import { Icon } from '../../components/Icon'
import { Readout } from '../../components/Readout'
import { RequestMap } from '../../components/RequestMap'
import { Verbatim } from '../../components/Verbatim'
import { useKeyedResource } from '../../state/backend'
import { useCluster, useProviders } from '../../state/resources'
import { plainClick, useRouter, type Route } from '../../state/router'
import { count } from '../keys/rows'
import { MAP_ATTRIBUTION, coordText, databaseSentence, detailZoom, pinRanges, placeLabel, precisionLabel, routeView } from './origins'
import { PinPlace } from './PinPlace'
import {
  attemptsLine,
  cacheHit,
  clientDevice,
  costLine,
  describeUserAgent,
  fmtMs,
  headline,
  rawFields,
  statusPill,
  timeSplit,
  tokenSplit,
} from './requestView'

const dash = (v: string | number | null | undefined): string =>
  v == null || v === '' ? '—' : String(v)

type Go = (e: MouseEvent, patch: Partial<Route>) => void

/** Segment fills, by token. Never text inside a segment: `--on-signal` on
 *  these is not a promised text pair, so the legend carries the labels. */
const TIME_FILL = { queue: 'var(--edge)', prefill: 'var(--flow)', decode: 'var(--stream)', total: 'var(--flow)' }
const TOKEN_FILL = { cached: 'var(--rule-strong)', new: 'var(--edge)', output: 'var(--batch)' }

/** `/usage/requests/<id>`: everything recorded about one client request, and each
 *  attempt the gateway made to answer it. */
export function RequestDetail({ requestId }: { requestId: string }) {
  const { navigate, linkTo } = useRouter()
  // 5 s, the coordinator's journal ship interval (telemetry/config.py
  // SHIP_INTERVAL_S): a link followed the moment a request finishes -- the
  // setup screen's test request, a chat turn -- arrives before the row does,
  // and should turn into the row on its own rather than after half a minute.
  const res = useKeyedResource(
    requestId,
    (b) => b.requests({ request_id: requestId, start: '-30d', page_size: 1 }),
    5_000,
  )

  const go: Go = (e, patch) => {
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
  // Keyed so an open pin form, a copied label or an open raw table does not
  // follow the reader to another request.
  return <Detail key={row.request_id} row={row} back={back} go={go} />
}

/** The page for one row. Owns the client's location, the route, the pin form
 *  and the two toggles. An address nothing places (a LAN or tailnet one) opens
 *  the pin form at once: a page with no map at all gave no hint that one could
 *  be drawn. */
function Detail({ row, back, go }: { row: RequestRow; back: ReactNode; go: Go }) {
  const { linkTo } = useRouter()
  const providers = useProviders().data ?? []
  const cluster = useCluster().data
  const ip = row.client_ip || ''
  const geo = useKeyedResource<GeoLookup | null>(
    `geo:${ip}`,
    (b) => (ip ? b.geoip(ip) : Promise.resolve(null)),
    // Not 0: useKeyedResource polls with setInterval, so 0 would spin. A
    // location only changes with a database download.
    600_000,
  )
  const g = geo.data
  const loc = g?.location ?? null
  // null until the reader chooses: the default is open exactly when there is
  // no location to show.
  const [pinning, setPinning] = useState<boolean | null>(null)
  const canPin = !!g && g.kind !== 'invalid' && pinRanges(ip).length > 0
  const open = canPin && (pinning ?? !loc)
  const route = useKeyedResource<RequestRoute>(
    `route:${row.request_id}`,
    (b) => b.requestRoute(row.request_id),
    600_000,
  )
  const [copied, setCopied] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])
  const copy = async () => {
    setCopied(await copyToClipboard(JSON.stringify(row, null, 2)))
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setCopied('idle'), 2000)
  }
  const [showRaw, setShowRaw] = useState(false)
  // The enlarged map, full width above the strip (every map on this page sits above it). It scales up from the
  // header map's corner and back down into it: `closing` keeps it mounted
  // until its shrink animation ends, then the header map settles back in
  // (`mapBack`; not on first load). Reduced motion clamps both to an instant
  // in base.css, and `animationend` still fires. Esc folds it back.
  const [bigMap, setBigMap] = useState<'closed' | 'open' | 'closing'>('closed')
  const [mapBack, setMapBack] = useState(false)
  const closeMap = () => setBigMap((s) => (s === 'open' ? 'closing' : s))
  useEffect(() => {
    if (bigMap !== 'open') return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeMap()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [bigMap])

  // The route's places once it has arrived and placed anything, else the
  // client's alone: a route whose hops nothing places must not hide a client
  // the location lookup did place.
  const routed = route.data ? routeView(route.data.hops) : null
  const view = routed?.points.length
    ? routed
    : { points: loc ? [{ ...loc, requests: 1, ips: 1, last_ts: row.ts }] : [], path: [] }
  const only = view.points.length === 1 ? view.points[0]! : null
  const device = clientDevice(loc?.label)
  const coords = loc ? `${coordText(loc.lat, loc.lon, loc.source)} · ${precisionLabel(loc.source)}` : ''
  // A known place answers for any address, local ones included, so it is
  // tested before the local-network sentence.
  const unplaced: string =
    g?.kind === 'local'
      ? 'local network'
      : g?.kind === 'public'
        ? g.database.state === 'ready' ? 'not in the location database' : databaseSentence(g.database)
        : '—'
  // One map, drawn at two sizes: the header's and the enlarged one.
  const mapProps = {
    title: `Where ${ip || 'this request'} and the servers that answered it are`,
    points: view.points,
    path: view.path,
    focus: only ? { lat: only.lat, lon: only.lon, zoom: detailZoom(only.source) } : undefined,
    ring: only && only.source !== 'place' ? { lat: only.lat, lon: only.lon } : undefined,
    fitMaxZoom: 13,
    scale: true,
    attribution: MAP_ATTRIBUTION,
  }
  const mapCaption = device ? `${device.device} · ${device.where}` : loc ? `${placeLabel(loc)} · ${coords}` : unplaced
  const location: ReactNode = !ip
    ? '—'
    : geo.error
      ? <Verbatim text={geo.error.message} size="unit" />
      : !g
        ? <span className="unit">…</span>
        : loc
          ? <>{device ? device.where : placeLabel(loc)}<span className="unit">{` · ${coords}`}</span></>
          : unplaced
  // The headline's place phrase. A `coordinator` point is a LAN, loopback or
  // tailnet address drawn at this coordinator's own city: the client is on
  // its network, not merely in that city.
  const where = device
    ? device.short
    : !loc
      ? null
      : loc.source === 'place'
        ? loc.label
        : loc.source === 'coordinator'
          ? "on this coordinator's network"
          : [loc.city, loc.region, loc.country].filter(Boolean).join(', ') || null
  const provider = providers.find((p) => p.provider_id === row.provider_id)
  const providerName = provider?.display_name ?? null

  const pill = statusPill(row)
  const keyId = row.key_id
  const rewritten = !!row.model_requested && row.model_requested !== (row.model_served ?? row.served_name)
  const time = timeSplit(row)
  const tokens = tokenSplit(row)
  const cache = cacheHit(row)
  const cost = costLine(row)
  const raw = rawFields(row)

  const nodeGone =
    !!cluster && !!row.node_id && !cluster.nodes.some((n) => n.profile.node_id === row.node_id)
  const dep = cluster?.deployments.find((d) => d.deployment_id === row.deployment_id)
  const depPill =
    !cluster || !row.deployment_id
      ? null
      : !dep
        ? 'deleted'
        : dep.state !== 'ready' && dep.state !== 'degraded'
          ? dep.state
          : null

  const chain: NonNullable<RequestRow['attempt_chain']> = row.attempt_chain ?? [
    {
      target_id: row.target_id,
      status: row.status,
      error_class: row.error_class || null,
      retry_reason: row.retry_reason || null,
      ms: row.duration_ms,
    },
  ]
  const showAttempts = chain.length > 1 || chain.some((a) => a.error_message || a.retry_after_s != null || a.retry_reason)

  const address = ip ? `${ip.includes(':') && row.client_port != null ? `[${ip}]` : ip}${row.client_port != null ? `:${row.client_port}` : ''}` : '—'
  const messages = row.message_count == null ? '—' : `${count(row.message_count)} ${row.message_count === 1 ? 'message' : 'messages'}`
  const spec =
    row.spec_decode_error === 'not_enabled'
      ? 'off'
      : row.spec_acceptance_rate != null
        ? `${Math.round(row.spec_acceptance_rate * 100)}% accepted · ${count(row.spec_accepted_tokens)} of ${count(row.spec_draft_tokens)} drafted`
        : dash(row.spec_decode_error)
  const ttftShare = row.ttft_ms != null && row.duration_ms ? `${Math.round((row.ttft_ms / row.duration_ms) * 100)}% of total` : 'not recorded'
  const totalTokens = (row.prompt_tokens ?? 0) + (row.completion_tokens ?? 0)

  return (
    <div className="card2">
      <div className="rq-head">
        <div className="rq-head-main">
          {back}
          <div className="rq-title" style={{ marginTop: 'var(--space-8)' }}>
            <h3 className="mono" style={{ margin: 0 }}>
              {row.request_id}
            </h3>
            <span className={`pill mono${pill.tone === 'muted' ? '' : ` ${pill.tone}`}`}>{pill.text}</span>
          </div>
          <p className="unit">
            {new Date(row.ts * 1000).toLocaleString()} ·{' '}
            {keyId ? (
              <>
                key{' '}
                <a
                  href={linkTo({ dest: 'usage', sub: 'keys', key: keyId })}
                  onClick={(e) => go(e, { dest: 'usage', sub: 'keys', key: keyId })}
                >
                  {row.key_name ?? keyId}
                </a>
              </>
            ) : (
              'no key'
            )}{' '}
            · policy <span className="mono">{dash(row.policy)}</span>
          </p>
          <p className="mono" style={{ margin: 0 }}>
            {row.served_name || row.model_served || '—'}{' '}
            <span className="unit">{rewritten ? `rewritten from ${row.model_requested}` : 'not rewritten'}</span>
          </p>
        </div>
        {!open && bigMap === 'closed' && view.points.length ? (
          <div className={`rq-head-map${mapBack ? ' rq-map-back' : ''}`}>
            <RequestMap {...mapProps} height={200} />
            <div className="rq-map-caption">
              <span className="unit">{mapCaption}</span>
              <button type="button" className="ghost" onClick={() => setBigMap('open')}>
                Enlarge map
              </button>
            </div>
          </div>
        ) : null}
      </div>
      {open ? <PinPlace ip={ip} loc={loc} onDone={() => setPinning(false)} /> : null}
      {!open && bigMap !== 'closed' && view.points.length ? (
        <div
          className={`rq-map-big${bigMap === 'closing' ? ' rq-map-closing' : ''}`}
          onAnimationEnd={(e) => {
            if (e.target !== e.currentTarget || bigMap !== 'closing') return
            setBigMap('closed')
            setMapBack(true)
          }}
        >
          <RequestMap {...mapProps} height={420} />
          <div className="rq-map-caption">
            <span className="unit">{mapCaption}</span>
            <button type="button" className="ghost" autoFocus onClick={closeMap}>
              Close map
            </button>
          </div>
        </div>
      ) : null}

      <div className="rq-strip">
        <p>{headline(row, { where, providerName })}</p>
        <div className="rq-actions">
          <button type="button" className="ghost" onClick={() => void copy()}>
            <Icon name="copy" size={12} decorative />{' '}
            {copied === 'copied' ? 'Copied' : copied === 'failed' ? 'Copy failed' : 'Copy as JSON'}
          </button>
          {canPin && !open ? (
            <button type="button" className="ghost" onClick={() => setPinning(true)}>
              Pin this address
            </button>
          ) : null}
        </div>
      </div>

      <div className="rq-stats">
        <Stat title="Total time" caption={row.parked_ms == null ? 'not queued' : `queued ${fmtMs(row.parked_ms)} before this`}>
          <Ms ms={row.duration_ms} />
        </Stat>
        <Stat title="Time to first token" caption={ttftShare}>
          <Ms ms={row.ttft_ms} />
        </Stat>
        <Stat
          title="Decode speed"
          caption={row.tpot_p95_ms != null ? `TPOT p95 ${fmtMs(row.tpot_p95_ms)}` : 'TPOT not recorded'}
        >
          <Readout size="readout" align="left" width={3} value={row.tokens_per_s} unit="tok/s" />
        </Stat>
        <Stat
          title="Tokens in → out"
          caption={
            `${count(totalTokens)} total` +
            (row.tokens_estimated ? ' · prompt estimated' : '') +
            (row.reasoning_tokens != null ? ` · ${count(row.reasoning_tokens)} reasoning` : '')
          }
        >
          <span className="readout">{`${count(row.prompt_tokens)} → ${count(row.completion_tokens)}`}</span>
        </Stat>
        <Stat title="Prompt cache hit" caption={cache.caption}>
          <Readout size="readout" align="left" width={2} value={cache.pct} unit="%" />
          <ProportionBar
            value={cache.pct == null ? null : cache.pct / 100}
            height={6}
            label="share of prompt tokens read from cache"
          />
        </Stat>
      </div>

      <h4 className="label" style={{ marginTop: 'var(--space-16)' }}>
        {time ? `Where the ${fmtMs(time.total)} went` : 'Where the time went'}
      </h4>
      {!time ? (
        <p className="unit">
          {row.admission_code
            ? 'No timing was recorded: the request was refused before it reached a model.'
            : 'No timing was recorded: the server reported no timings.'}
        </p>
      ) : (
        <>
          <div className="split" role="img" aria-label={time.segments.map((s) => `${s.label} ${fmtMs(s.ms)}`).join(', ')}>
            {time.segments.map((s) => (
              <span key={s.key} style={{ width: `${(s.ms / time.total) * 100}%`, background: TIME_FILL[s.key], padding: 0 }} />
            ))}
          </div>
          <div className="rq-ticks" aria-hidden="true">
            {time.ticks.map((t, i) => (
              <span key={t.ms} style={i === time.ticks.length - 1 ? undefined : { left: `${t.at}%` }}>
                {t.text}
              </span>
            ))}
          </div>
          <div className="legend" style={{ marginTop: 'var(--space-8)' }}>
            {time.segments.map((s) => (
              <span key={s.key}>
                <i className="rq-swatch" style={{ background: TIME_FILL[s.key] }} />
                {`${s.label} · ${fmtMs(s.ms)}`}
              </span>
            ))}
            {row.parked_ms == null ? <span>queue · not queued</span> : null}
          </div>
        </>
      )}

      <h4 className="label" style={{ marginTop: 'var(--space-16)' }}>
        Tokens
      </h4>
      {!tokens ? (
        <p className="unit">No tokens were counted.</p>
      ) : (
        <>
          <div className="split" role="img" aria-label={tokens.segments.map((s) => `${s.label} ${count(s.n)}`).join(', ')}>
            {tokens.segments.map((s) => (
              <span key={s.key} style={{ width: `${(s.n / tokens.total) * 100}%`, background: TOKEN_FILL[s.key], padding: 0 }} />
            ))}
          </div>
          <div className="legend">
            {tokens.segments.map((s) => (
              <span key={s.key}>
                <i className="rq-swatch" style={{ background: TOKEN_FILL[s.key] }} />
                {`${s.label} · ${count(s.n)}`}
              </span>
            ))}
            {row.cache_read_tokens == null ? <span>{`cache · ${cache.caption}`}</span> : null}
          </div>
        </>
      )}

      <div className="rq-cols">
        <div className="rq-col">
          <h4 className="label">Routing</h4>
          <Fact k="Served by">
            {row.target_kind === 'remote' ? (
              <>
                {providerName ?? row.provider_id} <span className="mono unit">{row.target_id}</span>
              </>
            ) : (
              <>
                <span className="mono">{dash(row.node_id)}</span>
                {nodeGone ? <> <span className="pill warn">left cluster</span></> : null}
              </>
            )}
          </Fact>
          {row.target_kind !== 'remote' && row.deployment_id ? (
            <Fact k="Deployment">
              <span className="mono">{row.deployment_id}</span>
              {depPill ? <> <span className="pill warn">{depPill}</span></> : null}
            </Fact>
          ) : null}
          <Fact k="Attempts">{attemptsLine(row)}</Fact>
          {row.limit_source || row.admission_code ? (
            <Fact k="Admission">
              <span className="mono">{[row.limit_source, row.admission_code].filter(Boolean).join(' · ')}</span>
            </Fact>
          ) : null}
          {row.strength_source ? <Fact k="Strength">{row.strength_source}</Fact> : null}
        </div>

        <div className="rq-col">
          <h4 className="label">Client</h4>
          <Fact k="Device">
            {device ? `${device.device}, ${device.where}` : g?.kind === 'local' ? 'local network, not a tailnet device' : '—'}
          </Fact>
          <Fact k="Address">
            <span className="mono">{address}</span>
          </Fact>
          <Fact k="Location">{location}</Fact>
          <Fact k="Browser">
            <span title={row.user_agent ?? undefined}>{describeUserAgent(row.user_agent)}</span>
          </Fact>
          <Fact k="Request">
            {`${row.streaming ? 'streamed' : 'not streamed'} · ${messages} · ${count(row.body_bytes)} B` +
              (row.max_tokens_requested != null ? ` · max ${count(row.max_tokens_requested)} tokens` : '') +
              (row.cancelled ? ' · cancelled' : '')}
          </Fact>
        </div>

        <div className="rq-col">
          <h4 className="label">Cost</h4>
          <Fact k="This request">
            {cost.value}
            {row.cost_usd != null && cost.note ? <span className="unit">{` · ${cost.note}`}</span> : null}
          </Fact>
          {row.cost_usd == null && cost.note ? (
            <Fact k="Why">
              <span>
                {cost.note}
                {cost.priceFor ? (
                  <>
                    {' '}
                    <a
                      href={linkTo({ dest: 'settings', price: cost.priceFor })}
                      onClick={(e) => go(e, { dest: 'settings', price: cost.priceFor })}
                    >
                      Set a price
                    </a>
                  </>
                ) : null}
              </span>
            </Fact>
          ) : null}
          <Fact k="Context band">
            <span className="mono">{dash(row.context_bucket)}</span>
          </Fact>
          <Fact k="Speculation">{spec}</Fact>
        </div>
      </div>

      {showAttempts ? (
        <>
          <h4 className="label" style={{ marginTop: 'var(--space-16)' }}>
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
        </>
      ) : null}

      <RouteTable route={route.data} error={route.error} />

      <div style={{ marginTop: 'var(--space-16)' }}>
        <button type="button" className="ghost" aria-expanded={showRaw} onClick={() => setShowRaw((v) => !v)}>
          {showRaw ? 'Hide raw fields' : `Show all ${raw.length} raw fields`}
        </button>
      </div>
      {showRaw ? (
        <table className="rq-raw">
          <tbody>
            {raw.map(([k, v]) => (
              <tr key={k}>
                <th scope="row" className="mono">
                  {k}
                </th>
                <td className="mono">{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  )
}

function Stat({ title, caption, children }: { title: string; caption: string; children: ReactNode }) {
  return (
    <div className="rq-stat">
      <span className="unit">{title}</span>
      {children}
      <span className="unit">{caption}</span>
    </div>
  )
}

/** A duration as a readout: milliseconds under a second, `fmtMs` above. */
function Ms({ ms }: { ms: number | null }) {
  return ms != null && ms >= 1000 ? (
    <span className="readout">{fmtMs(ms)}</span>
  ) : (
    <Readout size="readout" align="left" width={3} value={ms} unit="ms" />
  )
}

function Fact({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="rq-fact">
      <span className="unit">{k}</span>
      <span>{children}</span>
    </div>
  )
}

/** Every server the request passed through, in order, where each one is, and
 *  what each attempt answered. A hop nothing places says why in its own row. */
function RouteTable({ route, error }: { route: RequestRoute | null; error: Error | null }) {
  return (
    <>
      <h4 className="label" style={{ marginTop: 'var(--space-12)' }}>
        Route
      </h4>
      {error ? <Verbatim text={error.message} size="unit" /> : null}
      {!route ? (
        error ? null : <div className="unit" aria-busy="true">Reading the route…</div>
      ) : (
        // Five columns of addresses and places: on a phone it scrolls
        // sideways rather than widening the page.
        <div style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th scope="col">Hop</th>
                <th scope="col">Server</th>
                <th scope="col">Address</th>
                <th scope="col">Location</th>
                <th scope="col" style={{ textAlign: 'right' }}>Result</th>
              </tr>
            </thead>
            <tbody>
              {route.hops.map((h, i) => (
                <RouteRow key={i} hop={h} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

function RouteRow({ hop: h }: { hop: RouteHop }) {
  const loc = h.location
  return (
    <tr>
      <td>{h.role === 'client' ? 'Client' : h.role === 'gateway' ? 'Gateway' : `Attempt ${h.attempt}`}</td>
      <td>
        <span className="mono">{h.name}</span>
        {h.host ? <div className="unit mono">{h.host}</div> : null}
      </td>
      <td className="mono">{h.address ?? '—'}</td>
      <td>
        {loc ? (
          <>
            {placeLabel(loc)}
            <span className="unit">{` · ${coordText(loc.lat, loc.lon, loc.source)} · ${precisionLabel(loc.source)}`}</span>
          </>
        ) : h.address ? (
          <span className="unit">no location yet: pin a range that covers it</span>
        ) : null}
        {h.note ? <div className="unit">{h.note}</div> : null}
      </td>
      <td className="num">
        {h.status != null ? h.status : '—'}
        {h.ms != null ? <span className="unit">{` · ${Math.round(h.ms)} ms`}</span> : null}
      </td>
    </tr>
  )
}
