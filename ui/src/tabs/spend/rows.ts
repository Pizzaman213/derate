// The Spend screen's arithmetic, kept out of the component so it can be
// checked against the live coordinator (`spend.check.mjs`) rather than only
// typechecked. Everything here is pure.
//
// The rule the whole file follows is the one `SpendTab.tsx` was written to
// enforce and then only enforced on the local half: **a number we do not know
// is `null`, never `0`.** Cloud spend arrives as a real float from
// `providers/serialization.py` -- `round(runtime.spend_today(now), 6)` is never
// None -- so "$0.00" comes back identically for a provider that served nothing
// and for one that served two hundred requests through models it publishes no
// price for. The provider port already counts the difference; this module is
// what finally reads it.

import type { Provider, ProviderModel } from '../../api/types'

/** How a dollar figure came to be, which decides how it may be described. */
export type SpendBasis =
  /** The provider told us what it charged. OpenRouter puts a `cost` in every
   *  usage block, after caching, long-context tiers and per-modality
   *  surcharges. This is a ledger entry, not a projection. */
  | 'metered'
  /** Computed here from the provider's published input/output rates. Right for
   *  a plain request; blind to a cached prompt or a long-context tier. */
  | 'estimated'
  /** Both, in the same day. */
  | 'mixed'
  /** Requests were served and none of them could be priced at all. */
  | 'unpriced'
  /** Nothing was served today. A real zero. */
  | 'idle'
  /** The port does no accounting. Not a zero, an absence. */
  | 'unknown'

export interface CloudSpend {
  /** Dollars, or null when the figure is not knowledge. */
  usd: number | null
  /** True when real traffic sits under this number unpriced, so `usd` is a
   *  lower bound rather than the total. */
  floor: boolean
  basis: SpendBasis
  /** Requests served through models the provider publishes no price for. */
  unpricedRequests: number
}

/** What one provider spent today, and what kind of claim that is. */
export function cloudSpend(p: Provider): CloudSpend {
  const requests = p.requests_today
  // A port with no accounting nulls every field in the group at once
  // (gateway/ui_detail.py `_UNKNOWN_SPEND`). Nobody is counting; say so.
  if (requests == null || p.spend_today_usd == null) {
    return { usd: null, floor: false, basis: 'unknown', unpricedRequests: 0 }
  }
  if (requests === 0) {
    return { usd: 0, floor: false, basis: 'idle', unpricedRequests: 0 }
  }
  const unpriced = p.unpriced_requests_today ?? 0
  const metered = p.metered_requests_today ?? 0
  // Every request went through a model with no published price. The $0.00 the
  // wire carries is the absence of a rate card, not the absence of a bill --
  // and reporting it as spend is the one thing this screen must not do.
  if (unpriced >= requests) {
    return { usd: null, floor: false, basis: 'unpriced', unpricedRequests: unpriced }
  }
  const pricedRequests = requests - unpriced
  const basis: SpendBasis =
    metered >= pricedRequests ? 'metered' : metered === 0 ? 'estimated' : 'mixed'
  return {
    usd: p.spend_today_usd,
    floor: unpriced > 0,
    basis,
    unpricedRequests: unpriced,
  }
}

export interface FleetSpend {
  usd: number | null
  floor: boolean
  /** Providers whose spend is entirely unknown -- unpriced or unaccounted.
   *  Their traffic is real and is missing from `usd` altogether. */
  blindProviders: string[]
  unpricedRequests: number
  /** True once any dollar in `usd` came from a provider's own metering. */
  anyMetered: boolean
  /** True once any dollar in `usd` was computed from a published rate card. */
  anyEstimated: boolean
}

export function fleetSpend(providers: Provider[]): FleetSpend {
  let usd: number | null = null
  let floor = false
  let unpricedRequests = 0
  let anyMetered = false
  let anyEstimated = false
  const blindProviders: string[] = []
  for (const p of providers) {
    const s = cloudSpend(p)
    unpricedRequests += s.unpricedRequests
    if (s.floor) floor = true
    if (s.basis === 'metered' || s.basis === 'mixed') anyMetered = true
    if (s.basis === 'estimated' || s.basis === 'mixed') anyEstimated = true
    if (s.usd == null) {
      // 'unknown' is a port that does not account -- there may be no traffic
      // at all behind it. 'unpriced' is traffic we watched and could not
      // price, which is the case worth naming on screen.
      if (s.basis === 'unpriced') {
        blindProviders.push(p.provider_id)
        floor = true
      }
      continue
    }
    usd = (usd ?? 0) + s.usd
  }
  return { usd, floor, blindProviders, unpricedRequests, anyMetered, anyEstimated }
}

/** Output tokens a provider generated today. Null when nobody counted.
 *
 *  Output, not input+output, so it means the same thing as a local target's
 *  `counters.total_tokens` -- which `gateway/proxy.py` fills from the
 *  completion count alone. Summing the two halves here would make the "tokens
 *  generated" tile compare a cloud number against a local one measuring
 *  something else.
 */
export function cloudTokens(p: Provider): number | null {
  return p.tokens_today?.output ?? null
}

export interface PriceRange {
  low: number
  high: number
}

/** The $/Mtok across what a provider actually serves.
 *
 *  `Provider.models` and not the catalogue, and the difference is the whole
 *  point: `/api/providers` is served by `internal_api.py` through
 *  `_servable(...)`, which filters to what the operator switched on -- it names
 *  spend as one of its readers for exactly this reason. The unfiltered
 *  catalogue lives at `/api/providers/{id}/models` alone. Read from there
 *  instead, a provider serving one $0.12 model out of 428 would quote
 *  "$0.000-$600.000": true of OpenRouter's price list, and a wild misstatement
 *  of what anyone can be charged here.
 *
 *  The output rate, matching `gateway/targets.py::remote_cost_per_mtok` -- the
 *  figure the router itself weighs, on the reasoning that decode dominates a
 *  serving workload -- so the screen quotes the number the system acts on
 *  rather than a second opinion computed beside it. Falls back to the input
 *  rate only where a model publishes that and nothing else.
 */
export function publishedRange(models: ProviderModel[] | undefined): PriceRange | null {
  let low: number | null = null
  let high: number | null = null
  for (const m of models ?? []) {
    const rate = m.output_cost_per_mtok ?? m.input_cost_per_mtok
    if (rate == null) continue
    low = low == null ? rate : Math.min(low, rate)
    high = high == null ? rate : Math.max(high, rate)
  }
  if (low == null || high == null) return null
  return { low, high }
}

/** `$0.600`, or `$0.120–$6.000` when the served models do not agree on one.
 *
 *  A range rather than an average: a provider serving a cheap model and an
 *  expensive one has no single $/Mtok, and inventing the midpoint would be a
 *  number nobody is ever charged.
 */
export function formatRange(range: PriceRange | null): string {
  if (range == null) return '—'
  const at = (v: number) => `$${v.toFixed(3)}`
  return range.low === range.high ? at(range.low) : `${at(range.low)}–${at(range.high)}`
}

/** `$1.23`, or `≥ $1.23` when unpriced traffic sits underneath it. */
export function money(usd: number | null, floor = false): string {
  if (usd == null) return '—'
  return `${floor ? '≥ ' : ''}$${usd.toFixed(2)}`
}

/** The label under the spend figure, naming everything the figure leaves out.
 *
 *  The old string had one exclusion in it -- "cloud only", for an unset
 *  electricity rate -- and read as the whole truth in the other case it could
 *  not see. A total that silently omits a provider's unpriced traffic is the
 *  same failure the local half was fixed for, so both exclusions are listed by
 *  the same code and neither can be forgotten alone.
 */
export function spendCaption(rateSet: boolean, cloud: FleetSpend): string {
  const missing: string[] = []
  if (!rateSet) missing.push('local generation')
  if (cloud.blindProviders.length > 0) missing.push('unpriced cloud models')
  if (missing.length === 0) return 'spent today'
  return `spent today · excludes ${missing.join(' and ')}`
}

/** The muted lines under the caption: what to do, and what we could not price.
 *
 *  Each one names a specific thing rather than warning in general, because the
 *  first is an action the operator can take and the second is a fact about
 *  their own traffic.
 */
export function spendNotes(rateSet: boolean, cloud: FleetSpend): string[] {
  const notes: string[] = []
  if (!rateSet) notes.push('set an electricity rate to price local generation')
  if (cloud.blindProviders.length > 0) {
    const n = cloud.unpricedRequests
    notes.push(
      `${n.toLocaleString()} ${n === 1 ? 'request' : 'requests'} went to models ` +
        `${cloud.blindProviders.join(', ')} publishes no price for`,
    )
  }
  return notes
}

/** One sentence on where the dollars came from. Null when there are none. */
export function basisNote(cloud: FleetSpend, localPriced: boolean): string | null {
  const parts: string[] = []
  if (cloud.anyMetered) parts.push('cloud spend is what your provider reports it charged')
  if (cloud.anyEstimated) parts.push('some cloud spend is estimated from published rates')
  if (localPriced) parts.push('local cost is derived from measured power draw at your rate')
  if (parts.length === 0) return null
  return `${parts.join('; ')}.`
}
