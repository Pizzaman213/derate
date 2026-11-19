import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import marimekko, { mekko } from './index'

chartSuite({
  module: marimekko,
  dir: import.meta.dirname,
  role: 'segment',
  marks: { basic: 12, realistic: 28, 'edge-single': 1, 'edge-negatives': 6, 'edge-duplicates': 12, 'edge-large': 5200 },
  summary: /^Marimekko chart, 4 columns, 3 series, total 640, widest North at 3[78](\.5)?%$/,
  empty: ['edge-empty', 'edge-zeros'],
  malformed: [
    { name: 'a text value', data: [{ label: 'A', series: 'x', value: 1 }, { label: 'B', series: 'x', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ region: 'A', vendor: 'x', units: 1 }], options: { fields: { label: 'region', series: 'vendor', value: 'unit' } }, message: /column 'unit' not found/ },
  ],
})

const rows = [
  { label: 'A', series: 'x', value: 30 }, { label: 'A', series: 'y', value: 10 },
  { label: 'B', series: 'x', value: 5 }, { label: 'B', series: 'y', value: 15 }, { label: 'B', series: 'z', value: 20 },
  { label: 'C', series: 'z', value: 120 },
]

/** The plot rect as the segments cover it. */
function extent(el: Element) {
  const all = [...columns(el).values()].flat()
  const x = Math.min(...all.map((g) => g.x)), y = Math.min(...all.map((g) => g.y))
  return { x, y, width: Math.max(...all.map((g) => g.x + g.w)) - x, height: Math.max(...all.map((g) => g.y + g.h)) - y }
}

function columns(el: Element) {
  const by = new Map<string, Array<{ x: number; y: number; w: number; h: number; key: string }>>()
  for (const r of el.querySelectorAll('[data-role="segment"][data-mark]')) {
    const key = r.getAttribute('data-key')!.slice('rect:'.length)
    const col = key.split('\u2063')[0]!
    const g = { x: +r.getAttribute('x')!, y: +r.getAttribute('y')!, w: +r.getAttribute('width')!, h: +r.getAttribute('height')!, key }
    by.set(col, [...(by.get(col) ?? []), g])
  }
  return by
}

describe('marimekko: geometry', () => {
  it('column widths are proportional to column totals and sum to the plot width', () => {
    const el = host(640, 400)
    const h = marimekko.render(el, rows, { animate: false, height: 400 })
    const cols = columns(el)
    const width = (c: string) => cols.get(c)![0]!.w
    const sum = ['A', 'B', 'C'].reduce((s, c) => s + width(c), 0)
    const plot = extent(el)
    expect(sum).toBeCloseTo(plot.width, 1)
    expect(plot.x).toBeGreaterThan(20) // the y axis sits left of the plot
    expect(plot.x + plot.width).toBeLessThanOrEqual(640)
    expect(width('A') / sum).toBeCloseTo(40 / 200, 3)
    expect(width('B') / sum).toBeCloseTo(40 / 200, 3)
    expect(width('C') / sum).toBeCloseTo(120 / 200, 3)
    // adjacent, no gaps
    expect(cols.get('B')![0]!.x).toBeCloseTo(cols.get('A')![0]!.x + width('A'), 1)
    h.destroy()
  })
  it('segment heights are shares of their column and sum to the column height', () => {
    const el = host(640, 400)
    const h = marimekko.render(el, rows, { animate: false, height: 400 })
    const plot = extent(el)
    for (const [, segs] of columns(el)) expect(segs.reduce((s, g) => s + g.h, 0)).toBeCloseTo(plot.height, 1)
    const b = columns(el).get('B')!
    expect(b.map((g) => g.h / plot.height)).toEqual([5 / 40, 15 / 40, 20 / 40].map((v) => expect.closeTo(v, 3)))
    // stacked bottom-up, touching
    expect(b[0]!.y + b[0]!.h).toBeCloseTo(plot.y + plot.height, 1)
    expect(b[1]!.y + b[1]!.h).toBeCloseTo(b[0]!.y, 1)
    h.destroy()
  })
  it('sums duplicates, drops negatives and nulls, and notes all three', () => {
    const m = mekko([{ label: 'A', series: 'x', value: 1 }, { label: 'A', series: 'x', value: 2 }, { label: 'A', series: 'y', value: -4 }, { label: 'B', series: 'y', value: null }, { label: 'B', series: 'x', value: 5 }])
    expect(m.columns.map((c) => [c.label, c.total])).toEqual([['A', 3], ['B', 5]])
    expect([m.duplicates, m.negatives, m.nulls]).toEqual([1, 1, 1])
    const el = host()
    const h = marimekko.render(el, [{ label: 'A', series: 'x', value: 1 }, { label: 'A', series: 'x', value: 2 }, { label: 'A', series: 'y', value: -4 }], { animate: false })
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 negative values omitted; 1 duplicate label/series pairs summed')
    h.destroy()
  })
  it('hiding a series re-divides the columns without it', () => {
    const el = host(640, 400)
    const h = marimekko.render(el, rows, { animate: false, height: 400 })
    ;(el.querySelector('.oc-legend button[data-key="z"]') as HTMLElement).click()
    const cols = columns(el)
    expect([...cols.keys()]).toEqual(['A', 'B'])
    expect(cols.get('A')![0]!.w).toBeCloseTo(2 * cols.get('B')![0]!.w, 1)
    h.destroy()
  })
  it('sorts columns by total', () => {
    expect(mekko(rows, 'descending').columns.map((c) => c.label)).toEqual(['C', 'A', 'B'])
  })
})
