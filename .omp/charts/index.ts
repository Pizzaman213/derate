// The chart pack's one entry point.
//
//   chart(type, container, data, options)   render a chart by id
//   validate(type, data, options)           check data without rendering
//   listCharts({ category? })               what exists
//
// Each chart is one file in types/<id>/index.ts: its input schema, options and drawing.
// Rendering is TypeScript + D3 to SVG (canvas above 5k marks), themed by the design
// pack's tokens. See README.md; the rules for changing anything are in RULES.md.

import donut from './types/donut/index'
import groupedBar from './types/grouped-bar/index'
import multiLine from './types/multi-line/index'
import pie from './types/pie/index'
import stackedBar from './types/stacked-bar/index'
import stackedBar100 from './types/stacked-bar-100/index'
import { allModules, getModule } from './core/registry'
import type { AnyChartModule, Category, ChartHandle, Intent } from './core/types'

export * from './core'
export const ALL_CHARTS = [donut, groupedBar, multiLine, pie, stackedBar, stackedBar100]

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

function moduleFor(type: string): AnyChartModule {
  const id = resolveType(type)
  const mod = id ? getModule(id) : undefined
  if (!mod) {
    const known = allModules().map((m) => m.def.id)
    throw new Error(`unknown chart type '${type}' (have: ${known.join(', ')})`)
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

/** Charts, optionally filtered by category. */
export function listCharts(filter: { category?: string } = {}): ChartInfo[] {
  return allModules()
    .map((m) => m.def)
    .filter((d) => !filter.category || d.category === filter.category)
    .map((d) => ({ id: d.id, name: d.name, category: d.category, intents: d.intents, summary: d.summary, shape: d.shape, fields: d.fields ?? [], zoom: !!d.zoom }))
}
