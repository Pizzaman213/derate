// Small data-shaping steps several charts share.

export interface LabelValue {
  label: string
  value: number | null
}

/** Sum rows that share a label (first-seen order); nulls stay null unless a sibling has a value. */
export function collapseLabels<T extends { label: string; value: number | null }>(rows: T[]): { items: LabelValue[]; duplicates: number } {
  const map = new Map<string, LabelValue>()
  let duplicates = 0
  for (const r of rows) {
    const hit = map.get(r.label)
    if (!hit) map.set(r.label, { label: r.label, value: r.value })
    else {
      duplicates++
      hit.value = hit.value === null ? r.value : r.value === null ? hit.value : hit.value + r.value
    }
  }
  return { items: [...map.values()], duplicates }
}

/** Sort label/value items. */
export function sortItems<T extends { value: number | null }>(items: T[], order: 'none' | 'ascending' | 'descending'): T[] {
  if (order === 'none') return items
  const dir = order === 'ascending' ? 1 : -1
  return [...items].sort((a, b) => (a.value === null ? 1 : b.value === null ? -1 : dir * (a.value - b.value)))
}

/** Min/max of finite numbers, or null. */
export function finiteExtent(values: Iterable<number | null | undefined>): [number, number] | null {
  let lo = Infinity, hi = -Infinity
  for (const v of values) {
    if (typeof v !== 'number' || !Number.isFinite(v)) continue
    if (v < lo) lo = v
    if (v > hi) hi = v
  }
  return lo === Infinity ? null : [lo, hi]
}

/** Note text for collapsed duplicates / skipped values; undefined when there is nothing to say. */
export function notes(parts: Array<[number, string]>): string | undefined {
  const s = parts.filter(([n]) => n > 0).map(([n, what]) => `${n} ${what}`)
  return s.length ? s.join('; ') : undefined
}

/** Stable unique list of series names in first-seen order. */
export function uniq<T>(values: Iterable<T>): T[] {
  return [...new Set(values)]
}
