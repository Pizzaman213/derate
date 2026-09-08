import { useMemo } from 'react'
import type { RouteTarget } from '../api/types'
import { useProviders, useRouting, useSettings } from '../state/resources'
import type { SpendBasis } from './spend/rows'
import {
  basisNote,
  cloudSpend,
  cloudTokens,
  fleetSpend,
  formatRange,
  money,
  publishedRange,
  spendCaption,
  spendNotes,
} from './spend/rows'

// Ported from mockups-next/js/spend.js. Two inventions from that file are
// dead on arrival here: the "managed remote" tier (a target is local or a
// provider, never a third kind) collapses the split from three segments to
// two, and the 0.0009 Mtok/request * $0.60 "saved vs all-cloud" arithmetic
// is gone -- there is no such constant anywhere on the real wire, and a
// tile computed from one is exactly the kind of invented number this port
// exists to remove. "Tokens generated" replaces it with a real sum instead.
//
// The cloud half had the mirror-image version of the same bug for longer.
// `spend_today_usd` is `round(runtime.spend_today(now), 6)` server-side and is
// never None, so a provider serving models it publishes no price for reports
// exactly the "$0.00" of one serving nothing -- and the tile said "spent
// today" over both. `providers/runtime.py` has counted `unpriced_requests`
// all along; `spend/rows.ts` is what reads it. Where the provider prices the
// request itself -- OpenRouter puts a `cost` in every usage block -- that
// figure is banked instead of our rate-card arithmetic, and `metered_requests`
// is what lets this screen call one a charge and the other an estimate.
//
// Local cost is gated on the electricity rate the same way CostSection gates
// it: RouteTarget.cost_per_mtok is server-computed and comes back as a real
// 0.0 (gateway/targets.py `local_cost_per_mtok`), not null, when the rate is
// unset or <= 0 -- the settings default. Trusting the wire field directly
// here would render "$0.000" / "$0.00" out of the box, exactly the
// fabricated-zero this package exists to remove. So every local $/Mtok and
// spend figure below is re-gated on useSettings and forced to null (em dash)
// when no rate is configured, regardless of what cost_per_mtok says.
function targetLabel(t: RouteTarget): string {
  if (t.kind === 'local' && t.node_ids && t.node_ids.length > 0) return t.node_ids.join(' + ')
  return t.target_id
}

// A RoutingConfig can, in principle, alias the same physical target under
// two served names (e.g. two model aliases onto one upstream id), which
// would otherwise double-count its requests/tokens/spend when summed across
// configs. Dedupe by target_id -- first one wins -- before any aggregation.
function dedupeByTargetId(targets: RouteTarget[]): RouteTarget[] {
  const seen = new Map<string, RouteTarget>()
  for (const t of targets) {
    if (!seen.has(t.target_id)) seen.set(t.target_id, t)
  }
  return [...seen.values()]
}

interface Row {
  key: string
  name: string
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

export function SpendTab() {
  const routing = useRouting()
  const providers = useProviders()
  const settings = useSettings()

  const configs = routing.data ?? []
  const provs = providers.data ?? []
  const rateSet = (settings.data?.electricity_rate_usd_per_kwh ?? 0) > 0

  const localTargets = useMemo(
    () => dedupeByTargetId(configs.flatMap((c) => c.targets.filter((t) => t.kind === 'local'))),
    [configs],
  )
  const allTargets = useMemo(() => dedupeByTargetId(configs.flatMap((c) => c.targets)), [configs])

  // Two different accounting windows, on purpose: providers report a figure
  // that resets daily; the gateway keeps no history at all, so a local
  // target's counters are a running total since the process started. Adding
  // them is still the right total request count -- it just is not "today"
  // for both halves, which is exactly what the copy below says (and why the
  // tile itself is labelled "requests", not "requests today").
  const localRequests = localTargets.reduce((a, t) => a + (t.counters?.completed ?? 0), 0)
  const cloudRequests = provs.reduce((a, p) => a + (p.requests_today ?? 0), 0)
  const totalRequests = localRequests + cloudRequests

  // localSpend is null -- not 0 -- when there is no rate to price it with:
  // "never priced" and "priced at zero" must stay distinguishable here the
  // same way they do for a non-accounting provider port below.
  const localSpend = rateSet
    ? localTargets.reduce((a, t) => {
        const tok = t.counters?.total_tokens
        if (tok == null || t.cost_per_mtok == null) return a
        return a + (tok / 1_000_000) * t.cost_per_mtok
      }, 0)
    : null
  // Likewise, a fleet where every provider port does no spend accounting at
  // all must total to null, not the $0.00 that Array.reduce's seed would
  // otherwise produce -- that reads as "spent nothing" instead of "unknown".
  // `fleetSpend` additionally drops a provider whose every request went
  // through an unpriced model, and reports that its traffic is missing.
  const cloud = fleetSpend(provs)
  const totalSpend =
    localSpend == null && cloud.usd == null ? null : (localSpend ?? 0) + (cloud.usd ?? 0)

  // Remote targets carry no per-target counters -- the provider port accounts
  // per provider, not per model -- so a cloud request's tokens reach this tile
  // only through `tokens_today`. Without this the "tokens generated" figure
  // was local-only while sitting next to a total that included cloud spend.
  const localTokens = allTargets.reduce((a, t) => a + (t.counters?.total_tokens ?? 0), 0)
  const totalTokens = localTokens + provs.reduce((a, p) => a + (cloudTokens(p) ?? 0), 0)

  const pc = (v: number) => (totalRequests > 0 ? (v / totalRequests) * 100 : 0)
  const localPct = pc(localRequests)
  const cloudPct = pc(cloudRequests)

  const rows: Row[] = [
    ...localTargets.map((t) => {
      const tok = t.counters?.total_tokens
      const costPerMtok = rateSet ? t.cost_per_mtok : null
      return {
        key: t.target_id,
        name: targetLabel(t),
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

  return (
    <div>
      <div className="card2">
        <h3>Where requests went today</h3>
        <div className="unit">
          Provider counts reset daily; local counters run since the gateway started.
        </div>
        <div
          className="split"
          role="img"
          aria-label={`${localPct.toFixed(0)} percent local, ${cloudPct.toFixed(0)} percent cloud`}
        >
          <span style={{ width: `${localPct}%`, background: 'var(--fill)', color: 'var(--on-fill)' }}>
            {localPct >= 10 ? `local ${localPct.toFixed(0)}%` : ''}
          </span>
          <span
            style={{ width: `${cloudPct}%`, background: 'var(--warn-solid)', color: 'var(--on-signal)' }}
          >
            {cloudPct >= 10 ? `cloud ${cloudPct.toFixed(0)}%` : ''}
          </span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 18, marginTop: 14 }}>
          <div>
            <div className="big">{localPct.toFixed(0)}</div>
            <div className="unit">% local</div>
          </div>
          <div>
            <div className="big">{totalRequests.toLocaleString()}</div>
            <div className="unit">requests</div>
          </div>
          <div>
            {/* `≥` whenever real traffic sits under this number unpriced. A
                total that omits some of its own inputs is a lower bound, and
                rendering it as an equality is the fabricated-zero bug one
                decimal place further along. */}
            <div className="big">{money(totalSpend, cloud.floor)}</div>
            {/* Every exclusion, not just the electricity one: an unset rate
                leaves local generation out, and a provider serving models it
                publishes no price for leaves its whole bill out (wave-2 N2,
                and its cloud twin). */}
            <div className="unit">{spendCaption(rateSet, cloud)}</div>
            {totalSpend != null
              ? spendNotes(rateSet, cloud).map((note) => (
                  <div className="unit muted" key={note}>
                    {note}
                  </div>
                ))
              : null}
          </div>
          <div>
            <div className="big">{totalTokens.toLocaleString()}</div>
            <div className="unit">tokens generated</div>
          </div>
        </div>
      </div>

      <div className="card2">
        <h3>By target</h3>
        {/* Says which of the two kinds of number the column holds, because
            they are not the same claim: a metered figure is a charge, and an
            estimate is arithmetic over a published rate that cannot see a
            cached prompt or a long-context tier. */}
        <div className="unit" style={{ marginBottom: 10 }}>
          {basisNote(cloud, rateSet) ??
            'Local cost derives from measured power draw at your electricity rate.'}
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>Target</th>
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
                  <td colSpan={5} className="unit">
                    Nothing served yet.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
