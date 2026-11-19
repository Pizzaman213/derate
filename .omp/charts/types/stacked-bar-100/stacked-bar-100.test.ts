import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { makeFormatters } from '../../core'
import stackedBar100, { shares } from './index'
import { collapseSeries } from '../_shared/ComparisonA'

chartSuite({
  module: stackedBar100,
  dir: import.meta.dirname,
  role: 'bar',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 12, realistic: 24, 'edge-single': 1, 'edge-duplicates': 12, 'edge-large': 5200, 'edge-zero-category': 4 },
  summary: /^100% stacked bar chart, 4 bars of 3 series \(North, South and West\), largest share North in Q2 at 45%$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', series: 's', value: 1 }, { label: 'B', series: 's', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { label: ['a', 'b'], series: ['s', 's'], value: [1] }, message: /column 'value' has 1 values but column 'label' has 2/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', grp: 'x', amount: 1 }], options: { fields: { label: 'name', series: 'grp', value: 'amont' } }, message: /column 'amont' not found/ },
  ],
})

const rows = [
  { label: 'a', series: 'x', value: 1 }, { label: 'a', series: 'y', value: 2 }, { label: 'a', series: 'z', value: 7 },
  { label: 'b', series: 'x', value: 0.1 }, { label: 'b', series: 'y', value: 0.2 }, { label: 'b', series: 'z', value: 1e9 },
  { label: 'c', series: 'x', value: 0 }, { label: 'c', series: 'y', value: 0 }, { label: 'c', series: 'z', value: null },
  { label: 'd', series: 'x', value: 3 }, { label: 'd', series: 'y', value: -4 }, { label: 'd', series: 'z', value: 3 },
]
const num = (e: Element, a: string) => Number(e.getAttribute(a))

describe('stacked-bar-100: math', () => {
  it('every non-zero bar sums to 100% (within 1e-9); a zero-total bar sums to 0', () => {
    const t = collapseSeries(rows)
    const { layers, totals } = shares(t, t.series)
    for (const l of t.labels) {
      const sum = layers.reduce((acc, layer) => { const s = layer.spans.get(l)!; return acc + (s[1] - s[0]) }, 0)
      if ((totals.get(l) ?? 0) > 0) expect(Math.abs(sum - 1)).toBeLessThan(1e-9)
      else expect(sum).toBe(0)
    }
    const a = layers.map((layer) => layer.spans.get('a')!)
    expect(a[0]![1] - a[0]![0]).toBeCloseTo(0.1, 12)
    expect(a[2]![1] - a[2]![0]).toBeCloseTo(0.7, 12)
    // the negative is excluded from the whole: d is 50/0/50
    const d = layers.map((layer) => layer.spans.get('d')!)
    expect(d[0]![1] - d[0]![0]).toBeCloseTo(0.5, 12)
    expect(d[1]![1] - d[1]![0]).toBe(0)
  })
  it('bars fill the full value axis and segments are proportional to shares', () => {
    const el = host(640, 400)
    const h = stackedBar100.render(el, rows, { animate: false })
    const seg = (k: string) => el.querySelector(`[data-role="bar"][data-key="rect:${k}"]`)!
    const full = ['a::x', 'a::y', 'a::z'].reduce((s, k) => s + num(seg(k), 'height'), 0)
    const full2 = ['d::x', 'd::z'].reduce((s, k) => s + num(seg(k), 'height'), 0)
    expect(full2).toBeCloseTo(full, 1)
    expect(num(seg('a::z'), 'height') / num(seg('a::x'), 'height')).toBeCloseTo(7, 1)
    h.destroy()
  })
  it('draws an empty outline for the zero-total category and notes it and the negative', () => {
    const el = host(640, 400)
    const h = stackedBar100.render(el, rows, { animate: false })
    expect(el.querySelector('[data-key="rect:empty:c"]')).not.toBeNull()
    expect(el.querySelector('[data-key^="rect:c::"][data-mark]')).toBeNull()
    const note = el.querySelector('.oc-note')!.textContent!
    expect(note).toMatch(/1 category with a zero total left empty/)
    expect(note).toMatch(/1 negative values omitted/)
    h.destroy()
  })
  it('tooltip and announcement carry value and share', () => {
    const el = host(640, 400)
    const h = stackedBar100.render(el, rows, { animate: false })
    h.focusMark(2)
    expect(el.querySelector('[aria-live]')!.textContent).toMatch(/a, z: 70% \(7\)/)
    const fmt = makeFormatters({})
    expect(fmt.pct(0.7)).toBe('70%')
    h.destroy()
  })
  it('hiding a series re-expands the rest to 100%', () => {
    const el = host(640, 400)
    const h = stackedBar100.render(el, rows, { animate: false })
    const seg = (k: string) => el.querySelector(`[data-role="bar"][data-key="rect:${k}"][data-mark]`)!
    const before = num(seg('a::x'), 'height')
    el.querySelector<HTMLElement>('.oc-legend button[data-key="z"]')!.click()
    expect(num(seg('a::x'), 'height') / before).toBeCloseTo(1 / 0.3, 1)
    h.destroy()
  })
})
