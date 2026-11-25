// The KV width picker's rows, decided here rather than in the component so
// `kvWidths.check.mjs` can hold them (check.mjs bundles `platform: 'neutral'`,
// so nothing React may be imported).
//
// Every fact a row shows is the server's (`gateway/kv_widths.py`): which width
// the model's own is, each width's multiplier, the flag the launch passes and
// why a width is out. Nothing here re-derives one of them -- the client mirror
// of the 16-bit "no flag" rule went stale the day a checkpoint could declare
// its own cache width.

import type { PlanResponse } from '../../api/types'

/** What one plan answered for the picker. */
export type KvWidths = Pick<PlanResponse, 'kv_dtype_ratios' | 'kv_dtype_own' | 'kv_dtype_flags' | 'kv_dtype_refused'>

/** The widths offered when the runtime list has not loaded. */
export const FALLBACK_KV_NAMES = ['fp8', 'fp8_e4m3', 'fp8_e5m2']

export interface KvRow {
  /** The `kv_dtype` the row sends; '' is the model's own. */
  value: string
  label: string
  /** Cache bytes over the 16-bit rate; null when not known (yet). */
  ratio: number | null
  /** Refused for this plan: shown with a dash and `why`. */
  dashed: boolean
  /** The server's sentence, verbatim. */
  why: string | null
  /** What the launch passes for it, '' for nothing. */
  flag: string
  /** A dashed row cannot be picked, unless it is the one picked. */
  disabled: boolean
}

/** The model's own row's label: the width it stores, once a plan has said. */
export function ownLabel(widths: KvWidths | null): string {
  const own = widths?.kv_dtype_own
  if (!own?.name) return 'The model’s own (default)'
  return `The model’s own — ${own.name}${own.declared ? ', set by its quantization config' : ''}`
}

/** What the Memory summary says the cache is. */
export function kvSummary(selected: string, widths: KvWidths | null): string {
  const name = widths?.kv_dtype_own?.name
  return selected || (name ? `${name} (model’s own)` : 'default')
}

/** One row per width, the model's own first.
 *
 *  `flat` is `/api/runtimes`' model-free bytes per element for the plain
 *  widths: it stands in only until a plan has answered, because after that an
 *  absent ratio is a width this plan refuses and a multiplier would offer it. */
export function kvRows(
  names: string[],
  widths: KvWidths | null,
  flat: Record<string, number> | undefined,
  selected: string,
): KvRow[] {
  const ratios = widths?.kv_dtype_ratios ?? null
  const own: KvRow = {
    value: '',
    label: ownLabel(widths),
    ratio: widths?.kv_dtype_own?.ratio ?? null,
    dashed: false,
    why: null,
    // A declared width is read off the checkpoint by the engine; a 16-bit
    // own width needs no flag. Either way nothing is passed.
    flag: '',
    disabled: false,
  }
  const own16 = widths?.kv_dtype_own
  const rows = names
    .filter((name) => name !== 'auto')
    // The width that IS the model's own (16-bit, or the one its quantization
    // config declares), with no flag to pass, is the first row again under
    // another name; show it once. Kept when picked.
    .filter((name) => !(own16?.name === name && !widths?.kv_dtype_flags?.[name] && selected !== name))
    .map((name): KvRow => {
      const known = flat?.[name]
      const ratio = ratios != null ? (ratios[name] ?? null) : known != null ? known / 2 : null
      const dashed = ratios != null && ratio == null
      return {
        value: name,
        label: name,
        ratio,
        dashed,
        why: dashed ? (widths?.kv_dtype_refused?.[name] ?? null) : null,
        flag: widths?.kv_dtype_flags?.[name] ?? '',
        disabled: dashed && selected !== name,
      }
    })
  return [own, ...rows]
}

/** The rows to show: every offered width, plus a refused one only while it
 *  is the one picked, so it can be left. A width that does not work for this
 *  model is not shown at all. */
export function pickableKvRows(rows: KvRow[]): KvRow[] {
  return rows.filter((r) => !r.dashed || !r.disabled)
}
