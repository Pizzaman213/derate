import { useMemo, useState, type ReactNode } from 'react'
import type { LimitState, Provider, RouteTarget, RoutingConfig, UsageQuery, UsageResult } from '../../api/types'
import { ProportionBar } from '../../components/Bars'
import { Lamp } from '../../components/Lamp'
import { Verbatim } from '../../components/Verbatim'
import { useBackend, useKeyedResource, useResource } from '../../state/backend'
import { plainClick, useRouter } from '../../state/router'
import { SPEND_WINDOWS, type Route, type SpendWindow } from '../../state/routes'
import { useProviders, useRouting, useSettings } from '../../state/resources'
import { Chart, ChartGrid } from '../dashboard/Chart'
import { breakdown, count, EM, limitLamp, percent, type Breakdown } from '../keys/rows'
import type { SpendBasis } from './rows'
import { basisNote, cloudSpend, fleetSpend, formatRange, money, publishedRange } from './rows'
import {
  DEFAULT_WINDOW,
  ROW_CAP,
  budgetOf,
  budgetText,
  bucketFor,
  bySpend,
  cachedTitle,
  capRows,
  costSeries,
  floorOf,
  kindByModel,
  share,
  shareTitle,
  startFor,
  totalsBasis,
  totalsNotes,
  windowLabel,
  windowTotals,
  type WindowTotals,
} from './usage'

// Two sources, two different questions.
//
// Everything above "By target" reads `GET /api/usage` over the window in the
// URL (`?win=`, default `all`, which is the 1d rollups' 400-day retention):
// who sent the requests (per key), what they were sent to (per
// model), and the fleet's totals with a local/cloud split. That archive is
// where per-request cost was recorded -- `PricingStore` price rows for local
// models (Settings → Pricing), the provider's own metered `cost` where it
// sends one, its published rate card otherwise -- and `cost_usd` is null when
// nothing in a bucket was priced, so a key that only hit unpriced models is an
// em dash and never "$0.00". A total with priced dollars over unpriced
// requests is a floor and says `≥` (spend/usage.ts).
//
// "By target" is the rate card: the gateway's live per-target counters since
// it started and each provider's figures for today. Its local `$` is derived
// from measured power draw at the electricity rate (RouteTarget.cost_per_mtok)
// rather than from price rows, which is why a local target's dollars there
// need not match the same model's dollars in "By model".
//
// Local cost in By target is gated on the electricity rate the same way
// CostSection gates it: RouteTarget.cost_per_mtok is server-computed and comes
// back as a real 0.0 (gateway/targets.py `local_cost_per_mtok`), not null,
// when the rate is unset or <= 0 -- the settings default. Trusting the wire
// field directly here would render "$0.000" / "$0.00" out of the box, exactly
// the fabricated zero this screen exists to remove. So every local $/Mtok and
// spend figure there is re-gated on useSettings and forced to null (em dash)
// when no rate is configured, regardless of what cost_per_mtok says. Cloud
// spend in that table is `spend_today_usd`, which is never None server-side,
// so `spend/rows.ts::cloudSpend` reads `unpriced_requests_today` to tell a
// provider that served nothing from one that served only unpriced models.
function targetLabel(t: RouteTarget): string {
  if (t.kind === 'local' && t.node_ids && t.node_ids.length > 0) return t.node_ids.join(' + ')
  return t.target_id
}

// node_ids identifies the box, not the deployment -- two models colocated on
// the same node both label as that node's name (see targetLabel above), which
// reads as a duplicate row until the model column tells them apart. A target
// itself carries no served_name (RouteTarget has none), only the RoutingConfig
// wrapping it does, so that association has to be made here. A RoutingConfig
// can alias one physical target under two served names, so the first config
// to name a target_id wins and its requests are not counted twice.
interface LocalTarget {
  target: RouteTarget
  servedName: string
}

function localTargetsWithModel(configs: RoutingConfig[]): LocalTarget[] {
  const seen = new Map<string, LocalTarget>()
  for (const c of configs) {
    for (const t of c.targets) {
      if (t.kind !== 'local') continue
      if (!seen.has(t.target_id)) seen.set(t.target_id, { target: t, servedName: c.served_name })
    }
  }
  return [...seen.values()]
}

// A provider port can serve several models, so unlike a local target there is
// no single served_name to show -- same reasoning `priceLabel` already uses
// to render a range instead of one rate when the served models disagree.
function providerModelLabel(p: Provider): string {
  const names = p.models.map((m) => m.served_name)
  if (names.length === 0) return '—'
  if (names.length === 1) return names[0] ?? '—'
  return `${names.length} models`
}

interface Row {
  key: string
  name: string
  model: string
  kind: 'local' | 'cloud'
  requests: number | null
  /** Already formatted, because a cloud row's price is a range as often as it
   *  is a scalar and there is no one number to hand a `toFixed`. */
  priceLabel: string
  spend: number | null
  /** True when unpriced traffic sits under `spend`, making it a lower bound. */
  floor: boolean
  basis: SpendBasis
}

const USAGE_POLL_MS = 60_000
const LIST_POLL_MS = 30_000
const HEAD_STYLE = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'baseline',
  gap: 'var(--space-8)',
} as const

export function OverviewSub() {
  const routing = useRouting()
  const providers = useProviders()
  const settings = useSettings()
  const { route, navigate } = useRouter()
  const { backend } = useBackend()

  const win: SpendWindow = route.win ?? DEFAULT_WINDOW
  // A window is a selection, not a place: it replaces rather than pushes, and
  // the default spells itself as no parameter.
  const setWin = (w: SpendWindow) => navigate({ win: w === DEFAULT_WINDOW ? null : w }, { replace: true })
  const q: UsageQuery = { start: startFor(win), bucket: bucketFor(win) }

  const byProvider = useKeyedResource(`spend|${win}|provider`, (b) => b.usage({ ...q, group_by: ['provider'] }), USAGE_POLL_MS)
  const byKey = useKeyedResource(`spend|${win}|key`, (b) => b.usage({ ...q, group_by: ['key_id'] }), USAGE_POLL_MS)
  const byModel = useKeyedResource(
    `spend|${win}|model`,
    (b) => b.usage({ ...q, group_by: ['model', 'provider'] }),
    USAGE_POLL_MS,
  )
  const keys = useResource((b) => b.keys(), LIST_POLL_MS)

  const totals = useMemo(() => windowTotals(byProvider.data?.rows ?? []), [byProvider.data])
  const keyRows = useMemo(() => bySpend(breakdown(byKey.data?.rows ?? [], 'key_id')), [byKey.data])
  const modelRows = useMemo(() => bySpend(breakdown(byModel.data?.rows ?? [], 'model')), [byModel.data])
  const kinds = useMemo(() => kindByModel(byModel.data?.rows ?? []), [byModel.data])
  const keyById = useMemo(() => new Map((keys.data ?? []).map((k) => [k.key_id, k])), [keys.data])

  const configs = routing.data ?? []
  const provs = providers.data ?? []
  const rateSet = (settings.data?.electricity_rate_usd_per_kwh ?? 0) > 0

  const localTargets = useMemo(() => localTargetsWithModel(configs), [configs])
  // A fleet where every provider port does no spend accounting at all totals
  // to null, not the $0.00 a reduce's seed would produce. `fleetSpend` also
  // drops a provider whose every request went through an unpriced model.
  const cloud = fleetSpend(provs)

  const rows: Row[] = [
    ...localTargets.map(({ target: t, servedName }) => {
      const tok = t.counters?.total_tokens
      const costPerMtok = rateSet ? t.cost_per_mtok : null
      return {
        key: t.target_id,
        name: targetLabel(t),
        model: servedName,
        kind: 'local' as const,
        requests: t.counters?.completed ?? null,
        priceLabel: costPerMtok == null ? '—' : `$${costPerMtok.toFixed(3)}`,
        spend: tok != null && costPerMtok != null ? (tok / 1_000_000) * costPerMtok : null,
        floor: false,
        // Derived here from a rate the operator typed and a wattage we
        // measured. Never a bill anyone sent us.
        basis: 'estimated' as const,
      }
    }),
    ...provs.map((p) => {
      const s = cloudSpend(p)
      return {
        key: p.provider_id,
        name: p.provider_id,
        model: providerModelLabel(p),
        kind: 'cloud' as const,
        requests: p.requests_today,
        // The provider's own published rate for what it actually serves. One
        // number when the served models agree, a range when they do not, and
        // an em dash only when the provider publishes nothing -- which is a
        // different row from one that publishes $0.
        priceLabel: formatRange(publishedRange(p.models)),
        spend: s.usd,
        floor: s.floor,
        basis: s.basis,
      }
    }),
  ]

  const label = windowLabel(win)
  // The page renders from one set of answers or not at all. Seven reads feed
  // it, and drawing each card as its own read lands showed a page that
  // contradicted itself while it filled in: "Nothing served yet" under a
  // summary of requests, key ids that turned into names, a Budget column and
  // a price column that appeared a beat late. Settled means answered, either
  // way -- an error is drawn where its card is, not waited on forever.
  const settled = [byProvider, byKey, byModel, keys, routing, providers, settings].every(
    (r) => r.data != null || r.error != null,
  )
  const csv = (group_by: string[]) => backend.usageCsvUrl({ ...q, group_by })

  return (
    <div>
      <div className="card2">
        <h3 style={HEAD_STYLE}>
          <span>Spend, {label}</span>
          <span style={{ display: 'inline-flex', gap: 'var(--space-12)', alignItems: 'baseline' }}>
            <span className="chips" role="group" aria-label="Spend window">
              {SPEND_WINDOWS.map((w) => (
                <button type="button" key={w} aria-pressed={w === win} onClick={() => setWin(w)}>
                  {w}
                </button>
              ))}
            </span>
            <a className="unit" href={csv(['provider'])} download>
              CSV
            </a>
          </span>
        </h3>
        {!settled ? (
          <p className="unit" aria-busy="true" aria-live="polite">
            Reading usage…
          </p>
        ) : byProvider.error ? (
          <Verbatim text={byProvider.error.message} />
        ) : byProvider.data ? (
          <WindowSummary totals={totals} result={byProvider.data} />
        ) : null}
      </div>

      {settled ? (
        <>
          <BreakdownCard
            key={`key|${win}`}
            title="By key"
            note="Who sent the requests. A key is the unit of attribution; an admin's chat key is marked."
            csvUrl={csv(['key_id'])}
            error={byKey.error}
            loading={byKey.loading}
            loaded={byKey.data != null}
            rows={keyRows}
            totals={totals}
            empty={`No requests from any key, ${label}.`}
            lead={['Key']}
            leadCells={(k) => {
              const key = keyById.get(k.value)
              return (
                <td>
                  {k.value === EM ? (
                    EM
                  ) : (
                    <>
                      {/* A key `/api/keys` no longer lists (deleted) is named by
                      its id alone, rather than by its id twice. */}
                      <Go to={{ dest: 'usage', sub: 'keys', key: k.value }}>
                        {key ? key.name : <span className="mono">{k.value}</span>}
                      </Go>
                      {key ? <span className="mono unit"> {k.value}</span> : null}
                      {key?.is_admin_key ? (
                        <span className="unit"> · chat key of {key.admin_username ?? EM}</span>
                      ) : null}
                    </>
                  )}
                </td>
              )
            }}
            budget={(k) => keyById.get(k.value)?.limit_states}
          />

          <BreakdownCard
            key={`model|${win}`}
            title="By model"
            note="What the requests were sent to, local or through a provider."
            csvUrl={csv(['model', 'provider'])}
            error={byModel.error}
            loading={byModel.loading}
            loaded={byModel.data != null}
            rows={modelRows}
            totals={totals}
            empty={`No requests, ${label}.`}
            lead={['Model', 'Kind']}
            leadCells={(m) => (
              <>
                <td className="mono">{m.value}</td>
                <td className="unit">{kinds.get(m.value) ?? EM}</td>
              </>
            )}
            failed
          />

          <div className="card2">
            <h3>By target</h3>
            {/* Says which of the two kinds of number the column holds, because
            they are not the same claim: a metered figure is a charge, and an
            estimate is arithmetic over a published rate that cannot see a
            cached prompt or a long-context tier. */}
            <div className="unit" style={{ marginBottom: 'var(--space-10)' }}>
              {basisNote(cloud, rateSet) ??
                'Local cost derives from measured power draw at your electricity rate.'}
            </div>
            <div style={{ overflowX: 'auto' }}>
              <table>
                <thead>
                  <tr>
                    <th>Target</th>
                    <th>Model</th>
                    <th>Kind</th>
                    <th style={{ textAlign: 'right' }}>Requests</th>
                    <th style={{ textAlign: 'right' }}>$/Mtok out</th>
                    <th style={{ textAlign: 'right' }}>Spent</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.key}>
                      <td className="mono">{r.name}</td>
                      <td className="mono">{r.model}</td>
                      <td className="unit">{r.kind}</td>
                      <td className="num">{r.requests == null ? '—' : r.requests.toLocaleString()}</td>
                      <td className="num">{r.priceLabel}</td>
                      <td
                        className="num"
                        title={
                          r.basis === 'metered'
                            ? 'as the provider reported it charged'
                            : r.basis === 'mixed'
                              ? 'part as the provider reported it, part estimated'
                              : r.basis === 'unpriced'
                                ? 'this provider publishes no price for the models it served'
                                : r.kind === 'local'
                                  ? 'measured power draw at your electricity rate'
                                  : 'estimated from published rates'
                        }
                      >
                        {money(r.spend, r.floor)}
                      </td>
                    </tr>
                  ))}
                  {rows.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="unit">
                        Nothing served yet.
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </div>
        </>
      ) : null}
    </div>
  )
}

/** The window's totals: where the requests went, what they cost, and the two
 *  cost lines over time. */
function WindowSummary({
  totals,
  result,
}: {
  totals: WindowTotals
  result: UsageResult
}) {
  const localPct = totals.requests > 0 ? (totals.localRequests / totals.requests) * 100 : 0
  const cloudPct = totals.requests > 0 ? (totals.cloudRequests / totals.requests) * 100 : 0
  const local = costSeries(result.rows, 'local', result.from, result.to, result.bucket)
  const cloud = costSeries(result.rows, 'cloud', result.from, result.to, result.bucket)
  const note = `${local.length} × ${result.bucket}`
  const basis = totalsBasis(totals)

  return (
    <>
      <div
        className="split"
        role="img"
        aria-label={
          totals.requests === 0
            ? 'no requests'
            : `${localPct.toFixed(0)} percent local, ${cloudPct.toFixed(0)} percent cloud`
        }
      >
        <span style={{ width: `${localPct}%`, background: 'var(--fill)', color: 'var(--on-fill)' }}>
          {localPct >= 10 ? `local ${localPct.toFixed(0)}%` : ''}
        </span>
        <span style={{ width: `${cloudPct}%`, background: 'var(--warn-solid)', color: 'var(--on-signal)' }}>
          {cloudPct >= 10 ? `cloud ${cloudPct.toFixed(0)}%` : ''}
        </span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 'var(--space-18)', marginTop: 'var(--space-14)' }}>
        <div>
          {/* `≥` whenever real traffic sits under this number unpriced: a
              total that omits some of its own inputs is a lower bound. */}
          <div className="big">{money(totals.cost, totals.floor)}</div>
          <div className="unit">spent</div>
          {basis ? <div className="unit">{basis}</div> : null}
          {totalsNotes(totals).map((n) => (
            <div className="unit muted" key={n}>
              {n}
            </div>
          ))}
        </div>
        <div>
          <div className="big">{count(totals.requests)}</div>
          <div className="unit">requests</div>
        </div>
        <div>
          <div className="big">{count(totals.tokensOut)}</div>
          <div className="unit">tokens generated</div>
        </div>
        <div>
          <div className="big">{totals.cachedShare == null ? EM : `${percent(totals.cachedShare)}%`}</div>
          <div className="unit">input cached</div>
        </div>
      </div>
      <div style={{ marginTop: 'var(--space-14)' }}>
        <ChartGrid>
          <Chart title="Cost, local" unit="$" decimals={4} points={local} note={note} />
          <Chart title="Cost, cloud" unit="$" decimals={4} points={cloud} note={note} />
        </ChartGrid>
      </div>
    </>
  )
}

/** A real link to another screen: the browser's own middle-click and copy
 *  link address work, a plain click navigates in place. */
function Go({ to, children }: { to: Partial<Route>; children: ReactNode }) {
  const { navigate, linkTo } = useRouter()
  return (
    <a
      href={linkTo(to)}
      onClick={(e) => {
        if (!plainClick(e)) return
        e.preventDefault()
        navigate(to)
      }}
    >
      {children}
    </a>
  )
}

/** One cost-sorted breakdown, capped at ROW_CAP rows until "show all". Keyed
 *  by window at the call site, so a new window collapses it again. */
function BreakdownCard({
  title,
  note,
  csvUrl,
  error,
  loading,
  loaded,
  rows,
  totals,
  empty,
  lead,
  leadCells,
  failed = false,
  budget,
}: {
  title: string
  note: string
  csvUrl: string
  error: Error | null
  loading: boolean
  loaded: boolean
  rows: Breakdown[]
  totals: WindowTotals
  empty: string
  /** The headings of the columns that name the row. */
  lead: string[]
  leadCells: (b: Breakdown) => ReactNode
  failed?: boolean
  /** The row's configured limits, for a Budget column. Absent: no column. */
  budget?: (b: Breakdown) => LimitState[] | null | undefined
}) {
  const [expanded, setExpanded] = useState(false)
  const figures = ['Requests', ...(failed ? ['Failed'] : []), 'Tokens in', 'Cached', 'Tokens out', 'Reasoning', 'Cost']
  const colSpan = lead.length + figures.length + 1 + (budget ? 1 : 0)

  return (
    <div className="card2">
      <h3 style={HEAD_STYLE}>
        <span>{title}</span>
        <a className="unit" href={csvUrl} download>
          CSV
        </a>
      </h3>
      <div className="unit" style={{ marginBottom: 'var(--space-10)' }}>
        {note}
      </div>
      {error ? <Verbatim text={error.message} /> : null}
      <div style={{ overflowX: 'auto' }}>
        <table aria-busy={loading}>
          <thead>
            <tr>
              {lead.map((h) => (
                <th scope="col" key={h}>
                  {h}
                </th>
              ))}
              {figures.map((h) => (
                <th scope="col" className="num" key={h}>
                  {h}
                </th>
              ))}
              <th scope="col">Share</th>
              {budget ? <th scope="col">Budget</th> : null}
            </tr>
          </thead>
          <tbody>
            {loaded && rows.length === 0 ? (
              <tr>
                <td colSpan={colSpan} className="unit">
                  {empty}
                </td>
              </tr>
            ) : null}
            {capRows(rows, expanded).map((b) => {
              const s = share(b.cost, totals.cost)
              const shareText = shareTitle(s, share(b.requests, totals.requests))
              const limit = budget ? budgetOf(budget(b)) : null
              const lamp = limitLamp(limit)
              return (
                <tr key={b.value}>
                  {leadCells(b)}
                  <td className="num">{count(b.requests)}</td>
                  {failed ? <td className="num">{count(b.failed)}</td> : null}
                  <td className="num">{count(b.tokensIn)}</td>
                  <td className="num" title={cachedTitle(b.cacheRead, b.tokensIn) ?? undefined}>
                    {count(b.cacheRead)}
                  </td>
                  <td className="num">{count(b.tokensOut)}</td>
                  <td className="num">{count(b.reasoning)}</td>
                  <td className="num" title={b.unpriced > 0 ? `${b.unpriced} unpriced` : undefined}>
                    {money(b.cost, floorOf(b))}
                  </td>
                  <td>
                    <ProportionBar value={s} width={80} label={shareText} />
                  </td>
                  {budget ? (
                    <td>
                      <span style={{ display: 'inline-flex', gap: 'var(--space-6)', alignItems: 'center' }}>
                        <Lamp signal={lamp.signal} hollow={lamp.hollow} label={lamp.label} />
                        {budgetText(limit)}
                      </span>
                    </td>
                  ) : null}
                </tr>
              )
            })}
            {rows.length > ROW_CAP && !expanded ? (
              <tr>
                <td colSpan={colSpan} className="unit">
                  <button type="button" onClick={() => setExpanded(true)}>
                    show all {rows.length}
                  </button>
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  )
}
