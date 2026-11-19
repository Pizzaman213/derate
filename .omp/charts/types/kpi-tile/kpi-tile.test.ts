import { describe, expect, it } from 'vitest'
import { makeFormatters } from '../../core'
import { chartSuite, host } from '../../tests/harness'
import kpiTile, { deltaText, kpiDelta, seriesValues } from './index'

chartSuite({
  module: kpiTile,
  dir: import.meta.dirname,
  role: 'tile',
  marks: { basic: 1, realistic: 4, 'edge-single': 1, 'edge-zeros': 3, 'edge-nulls': 2, 'edge-duplicates': 2, 'edge-outliers': 2 },
  summary: /^KPI tile, MRR \$48,200, up \$3,100 \(6.9%\) vs previous, good$/,
  malformed: [
    { name: 'a text value', data: { label: 'x', value: 'lots' }, message: /field 'value' must be a number, got 'lots' at row 1/ },
    { name: 'a bad direction', data: { label: 'x', value: 1, goodDirection: 'sideways' }, message: /must be one of 'up', 'down', got 'sideways'/ },
    { name: 'a scalar', data: 7, message: /the data must be/ },
  ],
})

describe('kpi-tile: delta math', () => {
  it('absolute and percent change, coloured by the good direction', () => {
    expect(kpiDelta(110, 100)).toEqual({ abs: 10, pct: 0.1, dir: 'up', tone: 'good' })
    expect(kpiDelta(90, 100, 'down')).toEqual({ abs: -10, pct: -0.1, dir: 'down', tone: 'good' })
    expect(kpiDelta(90, 100, 'up')!.tone).toBe('bad')
    expect(kpiDelta(5, 5)).toEqual({ abs: 0, pct: 0, dir: 'flat', tone: 'neutral' })
    // percent against |previous|: -12k -> -4k is +66.7%
    expect(kpiDelta(-4000, -12000)!.pct).toBeCloseTo(2 / 3)
    expect(kpiDelta(3, 0)!.pct).toBeNull()
    expect(kpiDelta(null, 3)).toBeNull()
    expect(kpiDelta(3, null)).toBeNull()
  })
  it('formats signed change with a real minus sign', () => {
    const f = makeFormatters()
    expect(deltaText(kpiDelta(110, 100)!, f, undefined)).toBe('+10 (+10%)')
    expect(deltaText(kpiDelta(388, 412)!, f, 'ms')).toBe('−24 ms (−5.8%)')
    expect(deltaText(kpiDelta(3, 0)!, f, undefined)).toBe('+3')
    expect(deltaText(kpiDelta(1, 1)!, f, undefined)).toBe('no change')
  })
  it('sorts dated history and keeps plain numbers in order', () => {
    expect(seriesValues([3, 1, null, 2])).toEqual([3, 1, null, 2])
    expect(seriesValues([{ x: '2026-01-03', y: 3 }, { x: '2026-01-01', y: 1 }, { x: '2026-01-02', y: 2 }])).toEqual([1, 2, 3])
  })
  it('draws the arrow as a shape pointing the way the value moved', () => {
    const el = host(640, 200)
    const h = kpiTile.render(el, [{ label: 'a', value: 2, previous: 1 }, { label: 'b', value: 1, previous: 2, goodDirection: 'down' }], { animate: false })
    const up = el.querySelector('[data-key="path:a:arrow"]')!.getAttribute('d')!
    const down = el.querySelector('[data-key="path:b:arrow"]')!.getAttribute('d')!
    const ys = (d: string) => d.match(/-?[\d.]+/g)!.map(Number).filter((_, i) => i % 2 === 1)
    // up: the apex (2nd point) is the highest point; down: the lowest
    expect(ys(up)[1]).toBe(Math.min(...ys(up)))
    expect(ys(down)[1]).toBe(Math.max(...ys(down)))
    // both are good, so both use the positive colour
    expect(el.querySelector('[data-key="path:a:arrow"]')!.getAttribute('style')).toMatch(/--oc-positive/)
    expect(el.querySelector('[data-key="path:b:arrow"]')!.getAttribute('style')).toMatch(/--oc-positive/)
    h.destroy()
  })
  it('wraps a list into a grid that fits the width', () => {
    const el = host(400, 420)
    const h = kpiTile.render(el, Array.from({ length: 4 }, (_, i) => ({ label: `k${i}`, value: i })), { animate: false })
    const tiles = [...el.querySelectorAll('[data-role="tile"]')].map((t) => ({ x: Number(t.getAttribute('x')), y: Number(t.getAttribute('y')), w: Number(t.getAttribute('width')) }))
    expect(tiles.length).toBe(4)
    for (const t of tiles) expect(t.x + t.w).toBeLessThanOrEqual(400)
    expect(new Set(tiles.map((t) => t.y)).size).toBeGreaterThan(1)
    h.destroy()
  })
})
