import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { chartFor, recommend, inspect, INTENTS, listCharts } from '../index'
import { host } from '../tests/harness'

const DS = join(import.meta.dirname, '..', 'datasets')
const registry = JSON.parse(readFileSync(join(DS, 'datasets.json'), 'utf8')) as { datasets: Array<{ name: string; file: string }> }
const datasets = registry.datasets.map((d) => ({ name: d.name, data: (JSON.parse(readFileSync(join(DS, d.file), 'utf8')) as { data: unknown }).data }))

describe('chooser: every intent x every shared dataset gives a renderable pick', () => {
  for (const ds of datasets)
    for (const intent of INTENTS)
      it(`${ds.name} / ${intent}`, () => {
        const recs = recommend(intent, ds.data)
        expect(recs.length).toBeGreaterThan(0)
        for (const r of recs) {
          expect(r.reason.length).toBeGreaterThan(10)
          expect(listCharts().some((c) => c.id === r.type)).toBe(true)
        }
        const el = host(800, 480)
        const { handle, typeUsed, reason } = chartFor(intent, ds.data, el, { animate: false })
        try {
          expect(handle.errors).toEqual([])
          expect(handle.state).toBe('ok')
          expect(handle.markCount()).toBeGreaterThan(0)
          expect(typeUsed).toBe(recs[0]!.type)
          expect(reason).toBe(recs[0]!.reason)
        } finally {
          handle.destroy()
          el.remove()
        }
      })
})

describe('chooser: picks the right chart for the data', () => {
  const get = (n: string) => datasets.find((d) => d.name === n)!.data
  const top = (intent: string, data: unknown) => recommend(intent, data)[0]!.type
  it('sensible first choices on the shared datasets', () => {
    expect(top('trend', get('sales-by-month'))).toBe('multi-line')
    expect(top('compare', get('sales-by-month'))).toBe('bar')
    expect(top('schedule', get('project-timeline'))).toBe('gantt')
    expect(top('trend', get('ohlc-prices'))).toBe('candlestick')
    expect(top('hierarchy', get('org-tree'))).toBe('org-chart')
    expect(top('hierarchy', get('budget-breakdown'))).toBe('tree')
    expect(top('composition', get('budget-breakdown'))).toBe('treemap')
    expect(top('network', get('network-graph'))).toBe('force-network')
    expect(top('geo', get('population-by-region'))).toBe('choropleth')
    expect(top('distribution', get('server-metrics'))).toBe('box-plot')
    expect(top('correlation', get('population-by-region'))).toBe('correlation-matrix')
  })
  it('never a pie over 6 slices, never a dual axis unless asked', () => {
    const many = Array.from({ length: 9 }, (_, i) => ({ category: `c${i}`, amount: i + 1 }))
    const recs = recommend('composition', many)
    expect(recs.map((r) => r.type)).not.toContain('pie')
    expect(recs.map((r) => r.type)).not.toContain('donut')
    expect(recs[0]!.type).toBe('treemap')
    const few = many.slice(0, 4)
    expect(recommend('composition', few)[0]!.type).toBe('donut')
    for (const intent of INTENTS) for (const ds of datasets) expect(recommend(intent, ds.data).map((r) => r.type)).not.toContain('pareto')
  })
  it('lines only over an ordered x', () => {
    const unordered = [{ fruit: 'apple', n: 3 }, { fruit: 'pear', n: 5 }]
    expect(recommend('trend', unordered).map((r) => r.type)).not.toContain('line')
  })
  it('maps columns onto the chart fields', () => {
    const rows = [{ when: '2026-01-01', amount: 3 }, { when: '2026-02-01', amount: 5 }, { when: '2026-03-01', amount: 4 }]
    const r = recommend('trend', rows)[0]!
    expect(r.type).toBe('line')
    expect(r.data).toEqual([{ x: '2026-01-01', y: 3 }, { x: '2026-02-01', y: 5 }, { x: '2026-03-01', y: 4 }])
  })
  it('says when the intent does not fit and falls back to the natural view', () => {
    const r = recommend('sets', get('sales-by-month'))[0]!
    expect(r.reason).toMatch(/^no sets structure in this data/)
    expect(r.score).toBeLessThanOrEqual(40)
  })
  it('inspects shapes', () => {
    expect(inspect(get('network-graph')).kind).toBe('graph')
    expect(inspect(get('budget-breakdown')).kind).toBe('tree')
    const s = inspect(get('population-by-region'))
    expect(s.geo.map).toBe('world')
    expect(s.geo.lat).toBe('lat')
    expect(inspect([1, 2, 3]).kind).toBe('numbers')
    expect(inspect({ value: 42, max: 100 }).kind).toBe('kpi')
  })
  it('accepts intent aliases and rejects unknown ones', () => {
    expect(recommend('part-to-whole', get('budget-breakdown'))[0]!.type).toBe('treemap')
    expect(() => recommend('vibes', [])).toThrow(/unknown intent 'vibes'/)
  })
})
