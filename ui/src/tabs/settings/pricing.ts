// Local model pricing, as PricingCard edits it. Pure, so `pricing.check.mjs`
// can load it without a DOM.
//
// The rows are every model a LOCAL target serves now, plus every model that
// already has a price row -- a deployment that was stopped still has its price,
// and hiding it would make the price impossible to clear. A null rate is
// unpriced, never 0: `PricingStore.cost` estimates a local request only when
// BOTH rates are set, and an unpriced request counts toward no dollar limit.

import type { LocalPrice, ServedModel } from '../../api/types'

export interface PricingRow {
  model: string
  /** Served by a local target right now. */
  served: boolean
  price: LocalPrice | null
}

export function pricingRows(models: ServedModel[] | null, prices: LocalPrice[] | null): PricingRow[] {
  const byModel = new Map<string, PricingRow>()
  for (const m of models ?? []) {
    if (m.target_kinds.includes('local')) byModel.set(m.id, { model: m.id, served: true, price: null })
  }
  for (const p of prices ?? []) {
    const row = byModel.get(p.model)
    if (row) row.price = p
    else byModel.set(p.model, { model: p.model, served: false, price: p })
  }
  return [...byModel.values()].sort((a, b) => (a.model < b.model ? -1 : a.model > b.model ? 1 : 0))
}

/** A rate field's text -> the value to send. Blank is null (unpriced). Returns
 *  an error sentence for anything that is not a finite, non-negative number. */
export function parseRate(text: string): { value: number | null } | { error: string } {
  const t = text.trim()
  if (t === '') return { value: null }
  const n = Number(t)
  if (!Number.isFinite(n)) return { error: 'Enter a number of dollars, or leave it empty for unpriced.' }
  if (n < 0) return { error: 'A price cannot be negative.' }
  return { value: n }
}

/** A stored rate as the field's starting text: null is an empty field. */
export function rateText(v: number | null | undefined): string {
  return v == null ? '' : String(v)
}

/** Whether `PricingStore.cost` will price a local request on this row. */
export function isPriced(p: LocalPrice | null): boolean {
  return p != null && p.input_usd_per_mtok != null && p.output_usd_per_mtok != null
}
