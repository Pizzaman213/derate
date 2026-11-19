// The chart pack's one entry point.
//
//   chart(type, container, data, options)          render a chart by id
//   chartFor(intent, data, container, options)     let the chooser pick, render, explain
//   recommend(intent, data)                        ranked [{ type, score, reason }]
//   listCharts({ category?, intent? })             what exists
//   getSample(type, name) / getDataset(name)       premade inputs (after registerData)
//
// Rendering is TypeScript + D3 to SVG (canvas above 5k marks), themed by the design
// pack's tokens. See README.md; the rules for changing anything are in RULES.md.

import { ALL_CHARTS } from './types/all'
import { allModules, getModule } from './core/registry'
import type { AnyChartModule, Category, ChartHandle, Intent } from './core/types'
import { INTENT_ALIASES, INTENTS } from './core/types'
import { recommend as rank, inspect, describe as describeData, type Recommendation, type ChooserOptions } from './chooser/index'

export { inspect, describeData, type Recommendation, type ChooserOptions }

export * from './core'
export { ALL_CHARTS }

/** Chart ids that are spelled differently in conversation. */
export const ALIASES: Record<string, string> = {
  doughnut: 'donut',
}

/** Resolve an id or alias to a registered chart id, or undefined. */
export function resolveType(type: string): string | undefined {
  const t = type.trim().toLowerCase().replace(/[\s_]+/g, '-')
  if (getModule(t)) return t
  const a = ALIASES[t]
  return a && getModule(a) ? a : undefined
}

/** Normalise an intent spelling ("part-to-whole" -> composition). */
export function resolveIntent(intent: string): Intent | undefined {
  const t = intent.trim().toLowerCase().replace(/[\s_]+/g, '-')
  if ((INTENTS as string[]).includes(t)) return t as Intent
  return INTENT_ALIASES[t]
}

function moduleFor(type: string): AnyChartModule {
  const id = resolveType(type)
  const mod = id ? getModule(id) : undefined
  if (!mod) {
    const known = allModules().map((m) => m.def.id)
    const near = known.filter((k) => k.includes(type) || type.includes(k)).slice(0, 5)
    throw new Error(`unknown chart type '${type}'${near.length ? ` — did you mean ${near.join(', ')}?` : ''} (${known.length} types; listCharts() lists them)`)
  }
  return mod
}

/** Render chart `type` into `container`. */
export function chart(type: string, container: HTMLElement, data: unknown, options: Record<string, unknown> = {}): ChartHandle {
  return moduleFor(type).render(container, data, options)
}

/** Validate data for a chart type without rendering. */
export function validate(type: string, data: unknown, options: Record<string, unknown> = {}) {
  return moduleFor(type).validate(data, options)
}

export interface ChartInfo {
  id: string
  name: string
  category: Category
  intents: Intent[]
  summary: string
  shape: 'rows' | 'object'
  fields: string[]
  zoom: boolean
}

/** Charts, optionally filtered by category and/or intent. */
export function listCharts(filter: { category?: string; intent?: string } = {}): ChartInfo[] {
  const intent = filter.intent ? resolveIntent(filter.intent) ?? filter.intent : undefined
  return allModules()
    .map((m) => m.def)
    .filter((d) => !filter.category || d.category === filter.category)
    .filter((d) => !intent || (d.intents as string[]).includes(intent))
    .map((d) => ({ id: d.id, name: d.name, category: d.category, intents: d.intents, summary: d.summary, shape: d.shape, fields: d.fields ?? [], zoom: !!d.zoom }))
}

export interface Sample {
  description?: string
  data: unknown
  options?: Record<string, unknown>
}
interface DataBundle {
  samples: Record<string, Record<string, Sample>>
  datasets: Record<string, Sample & { name?: string }>
}
declare global {
  // dist/charts-data.js sets this when it loads before the API bundle
  // eslint-disable-next-line no-var
  var __ompChartsData: DataBundle | undefined
}
let bundle: DataBundle | null = globalThis.__ompChartsData ?? null

/** Make samples and datasets available (dist/charts-data.js calls this). */
export function registerData(d: DataBundle): void {
  bundle = d
}
function data(): DataBundle {
  if (!bundle) throw new Error('samples and datasets are not loaded: include dist/charts-data.js (or call registerData) first')
  return bundle
}

/** A premade sample for a chart ("basic", "realistic", "edge-nulls", ...). */
export function getSample(type: string, name = 'basic'): Sample {
  const id = resolveType(type) ?? type
  const s = data().samples[id]?.[name]
  if (!s) throw new Error(`no sample '${name}' for '${id}' (have: ${Object.keys(data().samples[id] ?? {}).join(', ') || 'none'})`)
  return s
}
/** Sample names for a chart. */
export function listSamples(type: string): string[] {
  const id = resolveType(type) ?? type
  return Object.keys(data().samples[id] ?? {})
}
/** A shared dataset by name (datasets/datasets.json). */
export function getDataset(name: string): Sample {
  const d = data().datasets[name]
  if (!d) throw new Error(`no dataset '${name}' (have: ${Object.keys(data().datasets).join(', ')})`)
  return d
}
/** Shared dataset names. */
export function listDatasets(): string[] {
  return Object.keys(data().datasets)
}

/** Ranked chart types for an intent and a dataset: [{ type, score, reason, options, data? }]. */
export function recommend(intent: string, data: unknown, options: ChooserOptions = {}): Recommendation[] {
  const i = resolveIntent(intent)
  if (!i) throw new Error(`unknown intent '${intent}' (one of: ${INTENTS.join(', ')})`)
  return rank(i, data, options)
}

/**
 * Let the chooser pick, then render. Tries the next recommendation if the first cannot
 * draw the data, so the result is a working chart or a clear error.
 */
export function chartFor(intent: string, data: unknown, container: HTMLElement, options: Record<string, unknown> = {}): { handle: ChartHandle; typeUsed: string; reason: string; alternatives: Recommendation[] } {
  const recs = recommend(intent, data)
  if (!recs.length) throw new Error(`no chart fits this data for '${intent}' (${describeData(inspect(data))})`)
  let last: { handle: ChartHandle; rec: Recommendation } | null = null
  for (const rec of recs) {
    const handle = chart(rec.type, container, rec.data ?? data, { ...rec.options, ...options })
    if (handle.state === 'ok') return { handle, typeUsed: rec.type, reason: rec.reason, alternatives: recs.filter((r) => r !== rec) }
    if (last) last.handle.destroy()
    last = { handle, rec }
  }
  const { handle, rec } = last!
  return { handle, typeUsed: rec.type, reason: `${rec.reason} (rendered in its ${handle.state} state: ${handle.errors.map((e) => e.message).join('; ') || 'no data'})`, alternatives: recs.filter((r) => r !== rec) }
}
