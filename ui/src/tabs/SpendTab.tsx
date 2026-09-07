import { useMemo } from 'react'
import type { RouteTarget } from '../api/types'
import { useProviders, useRouting, useSettings } from '../state/resources'

// Ported from mockups-next/js/spend.js. Two inventions from that file are
// dead on arrival here: the "managed remote" tier (a target is local or a
// provider, never a third kind) collapses the split from three segments to
// two, and the 0.0009 Mtok/request * $0.60 "saved vs all-cloud" arithmetic
// is gone -- there is no such constant anywhere on the real wire, and a
// tile computed from one is exactly the kind of invented number this port
// exists to remove. "Tokens generated" replaces it with a real sum instead.
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

function money(v: number | null): string {
  return v == null ? '—' : `$${v.toFixed(2)}`
}

interface Row {
  key: string
  name: string
  kind: 'local' | 'cloud'
  requests: number | null
  costPerMtok: number | null
  spend: number | null
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
  const cloudSpendKnown = provs.some((p) => p.spend_today_usd != null)
  const cloudSpend = cloudSpendKnown
    ? provs.reduce((a, p) => a + (p.spend_today_usd ?? 0), 0)
    : null
  const totalSpend = localSpend == null && cloudSpend == null ? null : (localSpend ?? 0) + (cloudSpend ?? 0)

  const totalTokens = allTargets.reduce((a, t) => a + (t.counters?.total_tokens ?? 0), 0)

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
        costPerMtok,
        spend: tok != null && costPerMtok != null ? (tok / 1_000_000) * costPerMtok : null,
      }
    }),
    ...provs.map((p) => ({
      key: p.provider_id,
      name: p.provider_id,
      kind: 'cloud' as const,
      requests: p.requests_today,
      costPerMtok: null,
      spend: p.spend_today_usd,
    })),
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
            <div className="big">{money(totalSpend)}</div>
            <div className="unit">spent today</div>
          </div>
          <div>
            <div className="big">{totalTokens.toLocaleString()}</div>
            <div className="unit">tokens generated</div>
          </div>
        </div>
      </div>

      <div className="card2">
        <h3>By target</h3>
        <div className="unit" style={{ marginBottom: 10 }}>
          Local cost derives from measured power draw at your electricity rate.
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>Target</th>
                <th>Kind</th>
                <th style={{ textAlign: 'right' }}>Requests</th>
                <th style={{ textAlign: 'right' }}>$/Mtok</th>
                <th style={{ textAlign: 'right' }}>Spent</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.key}>
                  <td className="mono">{r.name}</td>
                  <td className="unit">{r.kind}</td>
                  <td className="num">{r.requests == null ? '—' : r.requests.toLocaleString()}</td>
                  <td className="num">{r.costPerMtok == null ? '—' : `$${r.costPerMtok.toFixed(3)}`}</td>
                  <td className="num">{money(r.spend)}</td>
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
