import { describe, expect, it } from 'vitest'
import { chartSuite, host, loadSamples } from '../../tests/harness'
import cumulativeFlow, { stackFlow } from './index'

chartSuite({
  module: cumulativeFlow,
  dir: import.meta.dirname,
  role: 'segment',
  marks: { basic: 32, realistic: 100, 'edge-single': 4, 'edge-duplicates': 32, 'edge-large': 5200, 'edge-unsorted-dates': 32, 'edge-mixed-tz': 32, 'edge-long-labels': 32, 'edge-unicode-rtl': 32 },
  summary: /^Cumulative flow diagram, 4 states \(Backlog, In progress, Review and Done\), 8 dates from Mar 2 to Mar 9, 30 to 54 items in total, Done 0 to 30$/,
  empty: ['edge-empty', 'edge-zeros'],
  malformed: [
    { name: 'a text count', data: [{ date: '2026-03-02', state: 'Done', count: 1 }, { date: '2026-03-02', state: 'Doing', count: 'some' }], message: /field 'count' must be a number, got 'some' at row 2/ },
    { name: 'a bad date', data: [{ date: 'Q5', state: 'Done', count: 1 }], message: /field 'date' must be a date, got 'Q5' at row 1/ },
    { name: 'a wide table without a date', data: [{ day: '2026-03-02', Done: 1 }], message: /field 'date' is missing at row 1/ },
    { name: 'a mapping to a missing column', data: [{ day: '2026-03-02', status: 'Done', n: 1 }], options: { fields: { date: 'day', state: 'status', count: 'num' } }, message: /column 'num' not found/ },
  ],
})

const d = (s: string) => new Date(`${s}T00:00:00Z`)

describe('cumulative-flow: stacking', () => {
  it('stack tops equal the total WIP on every date', () => {
    for (const s of loadSamples(import.meta.dirname).filter((x) => !x.name.includes('empty'))) {
      const v = cumulativeFlow.validate(s.data, s.options)
      expect(v.ok).toBe(true)
      const m = stackFlow(v.data!, s.options?.states as string[] | undefined)
      m.dates.forEach((_, i) => {
        const sum = m.states.reduce((acc, st) => acc + (m.counts[i]!.get(st) ?? 0), 0)
        expect(m.totals[i]).toBe(sum)
        expect(m.layers[m.layers.length - 1]!.cells[i]!.y1).toBe(sum)
        // bands are contiguous: each layer starts where the one under it ends
        m.layers.forEach((l, k) => expect(l.cells[i]!.y0).toBe(k === 0 ? 0 : m.layers[k - 1]!.cells[i]!.y1))
      })
    }
  })
  it('stacks in reverse workflow order: the last state at the bottom', () => {
    const rows = [
      { date: d('2026-01-01'), state: 'Done', count: 5 },
      { date: d('2026-01-01'), state: 'Todo', count: 3 },
      { date: d('2026-01-01'), state: 'Doing', count: 2 },
    ]
    const m = stackFlow(rows, ['Todo', 'Doing', 'Done'])
    expect(m.states).toEqual(['Todo', 'Doing', 'Done'])
    expect(m.layers.map((l) => [l.state, l.cells[0]!.y0, l.cells[0]!.y1])).toEqual([['Done', 0, 5], ['Doing', 5, 7], ['Todo', 7, 10]])
  })
  it('sums repeated pairs, fills absent states with 0, clamps negatives, and restacks without hidden states', () => {
    const rows = [
      { date: d('2026-01-02'), state: 'A', count: 1 },
      { date: d('2026-01-01'), state: 'A', count: 2 },
      { date: d('2026-01-01'), state: 'A', count: 3 },
      { date: d('2026-01-01'), state: 'B', count: -4 },
      { date: d('2026-01-02'), state: 'B', count: null },
    ]
    const m = stackFlow(rows)
    expect(m.dates.map((x) => x.toISOString().slice(0, 10))).toEqual(['2026-01-01', '2026-01-02'])
    expect(m.totals).toEqual([5, 1])
    expect([m.duplicates, m.negatives, m.nulls, m.filled]).toEqual([1, 1, 1, 0])
    const hidden = stackFlow(rows, undefined, new Set(['B']))
    expect(hidden.layers.map((l) => l.state)).toEqual(['A'])
    expect(hidden.totals).toEqual([5, 1])
  })
  it('accepts wide rows (a column per state) and draws the top of the stack at the total', () => {
    const el = host(640, 400)
    const h = cumulativeFlow.render(el, 'when,Done,Doing,Todo\n2026-01-01,1,2,3\n2026-01-02,2,2,4\n', { animate: false, fields: { date: 'when' }, states: ['Todo', 'Doing', 'Done'] })
    expect(h.state).toBe('ok')
    expect(h.markCount()).toBe(6)
    expect(h.exportCSV().split('\n')).toEqual(['Date,Todo,Doing,Done,Total', '"Jan 1, 2026",3,2,1,6', '"Jan 2, 2026",4,2,2,8'])
    // the Todo segment is on top; its y is the total
    const top = el.querySelector('[data-key="rect:seg:Todo:' + Date.UTC(2026, 0, 2) + '"]')!
    const done = el.querySelector('[data-key="rect:seg:Done:' + Date.UTC(2026, 0, 2) + '"]')!
    const base = Number(done.getAttribute('y')) + Number(done.getAttribute('height'))
    const unit = Number(done.getAttribute('height')) / 2
    expect(base - Number(top.getAttribute('y'))).toBeCloseTo(8 * unit, 1)
    h.destroy()
  })
})
