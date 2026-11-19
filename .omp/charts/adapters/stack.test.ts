import { describe, expect, it } from 'vitest'
import { stackRows } from './stack'

const rows = [
  { x: 1, s: 'a', v: 1 },
  { x: 1, s: 'b', v: 2 },
  { x: 1, s: 'c', v: 3 },
  { x: 2, s: 'a', v: 4 },
  { x: 2, s: 'c', v: null }, // null -> 0, b missing -> 0
  { x: 3, s: 'a', v: 0 },
  { x: 3, s: 'b', v: 0 },
  { x: 2, s: 'a', v: 1 }, // duplicate (x, series) sums
]

describe('stackRows', () => {
  it('stacks so the last layer top equals the x total', () => {
    const out = stackRows(rows, { x: 'x', series: 's', value: 'v' })
    expect(out.keys).toEqual(['a', 'b', 'c'])
    expect(out.xs).toEqual([1, 2, 3])
    const top = out.layers.at(-1)?.points.map((p) => p.y1)
    expect(top).toEqual([6, 5, 0])
    expect(out.layers.at(-1)?.points.map((p) => p.total)).toEqual([6, 5, 0])
    expect(out.layers[1]?.points[1]).toEqual({ x: 2, y0: 5, y1: 5, value: 0, total: 5 })
    expect(out.layers[0]?.points[0]).toEqual({ x: 1, y0: 0, y1: 1, value: 1, total: 6 })
  })

  it('expand: tops sum to 1 per x, an all-zero x stays 0', () => {
    const out = stackRows(rows, { x: 'x', series: 's', value: 'v', offset: 'expand' })
    const tops = out.layers.at(-1)?.points.map((p) => p.y1) ?? []
    expect(Math.abs((tops[0] ?? 0) - 1)).toBeLessThan(1e-9)
    expect(Math.abs((tops[1] ?? 0) - 1)).toBeLessThan(1e-9)
    expect(tops[2]).toBe(0)
    for (const l of out.layers) for (const p of l.points) expect(Number.isNaN(p.y0) || Number.isNaN(p.y1)).toBe(false)
  })

  it('diverging: negatives go below 0, positives above', () => {
    const out = stackRows(
      [
        { x: 'q', s: 'gain', v: 3 },
        { x: 'q', s: 'loss', v: -2 },
        { x: 'q', s: 'more', v: 1 },
        { x: 'q', s: 'worse', v: -4 },
      ],
      { x: 'x', series: 's', value: 'v', offset: 'diverging' },
    )
    const byKey = Object.fromEntries(out.layers.map((l) => [l.key, l.points[0]]))
    expect([byKey.gain?.y0, byKey.gain?.y1]).toEqual([0, 3])
    expect([byKey.more?.y0, byKey.more?.y1]).toEqual([3, 4])
    expect([byKey.loss?.y0, byKey.loss?.y1]).toEqual([-2, 0])
    expect([byKey.worse?.y0, byKey.worse?.y1]).toEqual([-6, -2])
  })

  it('honours seriesOrder and appends unlisted series', () => {
    const out = stackRows(rows, { x: 'x', series: 's', value: 'v', seriesOrder: ['c', 'a'] })
    expect(out.keys).toEqual(['c', 'a', 'b'])
    expect(out.layers.map((l) => l.key)).toEqual(['c', 'a', 'b'])
    expect(out.layers[0]?.points[0]).toMatchObject({ y0: 0, y1: 3 })
  })

  it('order descending puts the largest series at the bottom', () => {
    const out = stackRows(rows, { x: 'x', series: 's', value: 'v', order: 'descending' })
    expect(out.layers.map((l) => l.key)).toEqual(['a', 'c', 'b'])
    expect(out.layers[0]?.points[0]).toMatchObject({ y0: 0, y1: 1 })
  })

  it('groups Date xs by time', () => {
    const out = stackRows(
      [
        { x: new Date(0), s: 'a', v: 1 },
        { x: new Date(0), s: 'b', v: 2 },
      ],
      { x: 'x', series: 's', value: 'v' },
    )
    expect(out.xs).toHaveLength(1)
    expect(out.layers[1]?.points[0]?.y1).toBe(3)
  })
})
