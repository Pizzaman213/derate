import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { makeFormatters } from '../../core'
import bump, { bumpModel, deriveRanks } from './index'

chartSuite({
  module: bump,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 20, realistic: 80, 'edge-single': 1, 'edge-duplicates': 20, 'edge-nulls': 17, 'edge-large': 5200, 'edge-mixed-tz': 20 },
  summary: /^Bump chart, 5 series over 4 points, 2021 to 2024, Echo ranked first at the end, biggest climb Echo \(up 4 places\)$/,
  malformed: [
    { name: 'a text rank', data: [{ x: '1', series: 'A', rank: 'first' }], message: /field 'rank' must be a number, got 'first' at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ w: 1, c: 'A', p: 1 }], options: { fields: { x: 'w', series: 'c', rank: 'pos' } }, message: /column 'pos' not found/ },
  ],
})

const fmt = makeFormatters()
const row = (x: string, series: string, value: number | null, rank: number | null = null) => ({ x, series, value, rank })

describe('bump: ranks', () => {
  it('derives ranks from values, highest first, ties share the better rank', () => {
    expect(deriveRanks([10, 30, 20])).toEqual([3, 1, 2])
    expect(deriveRanks([5, 9, 5, 1])).toEqual([2, 1, 2, 4])
    expect(deriveRanks([null, 2, 1])).toEqual([null, 1, 2])
  })
  it('ranks each x on its own', () => {
    const m = bumpModel([row('a', 'P', 1), row('a', 'Q', 2), row('b', 'P', 9), row('b', 'Q', 3)], fmt)
    const rank = (x: number, s: string) => m.cells.find((c) => c.x === x && c.series === s)!.rank
    expect([rank(0, 'P'), rank(0, 'Q'), rank(1, 'P'), rank(1, 'Q')]).toEqual([2, 1, 1, 2])
    expect(m.cells.every((c) => c.derived)).toBe(true)
  })
  it('uses given ranks as they are', () => {
    const m = bumpModel([row('a', 'P', 100, 2), row('a', 'Q', 1, 1)], fmt)
    expect(m.cells.map((c) => [c.series, c.rank])).toEqual([['Q', 1], ['P', 2]])
  })
  it('sorts numeric and date x, keeps ordinal labels in order of appearance', () => {
    expect(bumpModel([row('10', 'A', 1), row('9', 'A', 1)], fmt).xs.map((x) => x.label)).toEqual(['9', '10'])
    expect(bumpModel([row('2026-03-01', 'A', 1), row('2026-01-01', 'A', 1)], fmt).xs.length).toBe(2)
    expect(bumpModel([row('2026-03-01', 'A', 1), row('2026-01-01', 'A', 1)], fmt).xs[0]!.key).toBe(String(Date.UTC(2026, 0, 1)))
    expect(bumpModel([row('Week 2', 'A', 1), row('Week 1', 'A', 1)], fmt).xs.map((x) => x.label)).toEqual(['Week 2', 'Week 1'])
  })
  it('one instant written two ways is one x', () => {
    expect(bumpModel([row('2026-01-05T00:00:00Z', 'A', 1), row('2026-01-05T05:30:00+05:30', 'B', 2)], fmt).xs.length).toBe(1)
  })
  it('rank 1 is drawn at the top', () => {
    const el = host(600, 400)
    const h = bump.render(el, [row('a', 'P', 1), row('a', 'Q', 2)], { animate: false })
    const pts = [...el.querySelectorAll('[data-role="point"]')]
    const cy = (s: string) => Number(pts.find((p) => p.getAttribute('data-key')!.includes(`:${s}:`))!.getAttribute('cy'))
    expect(cy('Q')).toBeLessThan(cy('P'))
    h.destroy()
  })
})
