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
    expect(top('compare', get('sales-by-month'))).toBe('grouped-bar')
    expect(top('trend', get('server-metrics'))).toBe('multi-line')
    expect(top('rank', get('population-by-region'))).toBe('grouped-bar')
    expect(top('composition', get('survey-results'))).toBe('donut')
    expect(recommend('composition', get('survey-results')).map((r) => r.type)).toContain('stacked-bar-100')
  })
  it('never a pie over 6 slices or with negatives', () => {
    const many = Array.from({ length: 9 }, (_, i) => ({ category: `c${i}`, amount: i + 1 }))
    expect(recommend('composition', many).map((r) => r.type)).not.toContain('pie')
    expect(recommend('composition', many).map((r) => r.type)).not.toContain('donut')
    const few = many.slice(0, 4)
    expect(recommend('composition', few)[0]!.type).toBe('donut')
    const negative = [{ category: 'a', amount: 3 }, { category: 'b', amount: -1 }]
    expect(recommend('composition', negative).map((r) => r.type)).not.toContain('pie')
  })
  it('lines only over an ordered x', () => {
    const unordered = [{ fruit: 'apple', n: 3 }, { fruit: 'pear', n: 5 }]
    expect(recommend('trend', unordered).map((r) => r.type)).not.toContain('multi-line')
  })
  it('reshapes the columns into the chart input', () => {
    const rows = [{ when: '2026-01-01', amount: 3 }, { when: '2026-02-01', amount: 5 }, { when: '2026-03-01', amount: 4 }]
    const r = recommend('trend', rows)[0]!
    expect(r.type).toBe('multi-line')
    expect(r.data).toEqual([{ x: '2026-01-01', y: 3, series: 'amount' }, { x: '2026-02-01', y: 5, series: 'amount' }, { x: '2026-03-01', y: 4, series: 'amount' }])
  })
  it('ranks largest first, horizontally', () => {
    const r = recommend('rank', [{ team: 'a', score: 1 }, { team: 'b', score: 3 }, { team: 'a', score: 1 }])[0]!
    expect(r.options).toEqual({ orientation: 'horizontal' })
    expect(r.data).toEqual([{ label: 'b', value: 3, series: 'score' }, { label: 'a', value: 2, series: 'score' }])
  })
  it('says when the intent does not fit and falls back to the natural view', () => {
    const r = recommend('sets', get('sales-by-month'))[0]!
    expect(r.reason).toMatch(/^no sets structure in this data/)
    expect(r.score).toBeLessThanOrEqual(40)
  })
  it('inspects shapes', () => {
    expect(inspect(get('sales-by-month')).kind).toBe('rows')
    expect(inspect([]).kind).toBe('empty')
    expect(inspect([1, 2, 3]).kind).toBe('unknown')
    expect(recommend('compare', [1, 2, 3])).toEqual([])
  })
  it('accepts intent aliases and rejects unknown ones', () => {
    expect(recommend('part-to-whole', get('survey-results'))[0]!.type).toBe('donut')
    expect(() => recommend('vibes', [])).toThrow(/unknown intent 'vibes'/)
  })
})
