import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import histogram, { binSeries } from './index'
import type { HistogramOptions } from './schema'

const sample = (name: string) => JSON.parse(readFileSync(join(import.meta.dirname, 'samples', `${name}.json`), 'utf8')) as { data: unknown; options?: Record<string, unknown> }
const large = sample('edge-large')
const largeBins = binSeries(histogram.validate(large.data, large.options).data!, { bins: { count: 5200 } }).series[0]!.counts.filter((c) => c > 0).length

chartSuite({
  module: histogram,
  dir: import.meta.dirname,
  marks: { basic: 6, 'edge-single': 1, 'edge-zeros': 1, 'edge-large': largeBins, 'edge-unicode-rtl': 10 },
  role: 'bar',
  summary: /^Histogram, 40 values in 6 bins from 40 to 100, most in 70–80 \(11\)$/,
  malformed: [
    { name: 'a text value', data: [{ value: 1 }, { value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a text value in a number list', data: [1, 2, 'abc'], message: /field 'value' must be a number, got 'abc' at row 3/ },
    { name: 'a scalar', data: 42, message: /must be a list of numbers, a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ minutes: 1 }], options: { fields: { value: 'mins' } }, message: /column 'mins' not found/ },
  ],
})

const opts = (bins: HistogramOptions['bins'] = {}): Pick<HistogramOptions, 'bins'> => ({ bins })

describe('histogram: binning', () => {
  it('bin counts sum to n and match the known counts', () => {
    const data = histogram.validate(sample('basic').data).data!
    const b = binSeries(data, opts())
    expect(b.edges).toEqual([40, 50, 60, 70, 80, 90, 100])
    expect(b.series[0]!.counts).toEqual([2, 4, 7, 11, 9, 7])
    expect(b.series[0]!.counts.reduce((a, c) => a + c, 0)).toBe(40)
  })
  it('honours explicit thresholds and keeps the last bin closed', () => {
    const data = histogram.validate([0, 1, 2, 3, 10]).data!
    const b = binSeries(data, opts({ thresholds: [2, 4] }))
    expect(b.edges).toEqual([0, 2, 4, 10])
    expect(b.series[0]!.counts).toEqual([2, 2, 1])
  })
  it('a fixed domain leaves outside values out and says so', () => {
    const el = host()
    const h = histogram.render(el, [1, 2, 3, 50], { animate: false, bins: { domain: [0, 10], count: 2 } })
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 value outside the bin range left out/)
    h.destroy()
  })
  it('series share one set of edges and each sums to its n', () => {
    const s = sample('realistic')
    const data = histogram.validate(s.data, s.options).data!
    const b = binSeries(data, opts({ method: 'freedman-diaconis' }))
    expect(b.series.map((x) => x.counts.reduce((a, c) => a + c, 0))).toEqual([140, 180, 120])
    for (const x of b.series) expect(x.counts.length).toBe(b.edges.length - 1)
  })
  it('a single value gets one bin of non-zero width', () => {
    const b = binSeries(histogram.validate([7]).data!, opts())
    expect(b.edges.length).toBe(2)
    expect(b.edges[1]! - b.edges[0]!).toBeGreaterThan(0)
    expect(b.series[0]!.counts).toEqual([1])
  })
})

describe('histogram: geometry', () => {
  it('bar heights are proportional to counts and adjacent bars touch', () => {
    const el = host(640, 400)
    const h = histogram.render(el, sample('basic').data, { animate: false })
    const rects = [...el.querySelectorAll('[data-role="bar"]')]
    const hs = rects.map((r) => Number(r.getAttribute('height')))
    expect(hs[3]! / hs[0]!).toBeCloseTo(11 / 2, 1)
    const x = (i: number) => Number(rects[i]!.getAttribute('x'))
    const w = (i: number) => Number(rects[i]!.getAttribute('width'))
    expect(x(1) - (x(0) + w(0))).toBeLessThanOrEqual(1.01)
    h.destroy()
  })
  it('percent bars of two series of different size compare shares', () => {
    const el = host()
    const data = [...[1, 1, 2, 2].map((v) => ({ value: v, series: 'a' })), ...[1, 2, 2, 2, 2, 2, 2, 2].map((v) => ({ value: v, series: 'b' }))]
    const h = histogram.render(el, data, { animate: false, normalize: 'percent', bins: { thresholds: [1.5] } })
    const by = (k: string) => Number(el.querySelector(`[data-key="rect:${k}"]`)!.getAttribute('height'))
    // a: 50% / 50%; b: 12.5% / 87.5%
    expect(by('a:0') / by('b:0')).toBeCloseTo(4, 1)
    expect(el.querySelectorAll('.oc-legend button, .oc-legend [data-key]').length).toBeGreaterThan(0)
    h.destroy()
  })
  it('facet layout draws one panel per series with the same bars', () => {
    const el = host(800, 500)
    const s = sample('realistic')
    const h = histogram.render(el, s.data, { ...s.options, layout: 'facet', animate: false })
    const overlay = histogram.render(host(800, 500), s.data, { ...s.options, animate: false })
    expect(h.markCount()).toBe(overlay.markCount())
    expect(el.querySelectorAll('[data-key^="text:ft:"]').length).toBe(3)
    h.destroy()
    overlay.destroy()
  })
  it('density normalisation gives a total bar area of 1', () => {
    const el = host()
    const h = histogram.render(el, sample('basic').data, { animate: false, normalize: 'density' })
    const b = binSeries(histogram.validate(sample('basic').data).data!, opts())
    const area = b.series[0]!.counts.reduce((t, c, i) => t + (c / (40 * (b.edges[i + 1]! - b.edges[i]!))) * (b.edges[i + 1]! - b.edges[i]!), 0)
    expect(area).toBeCloseTo(1, 6)
    expect(h.markCount()).toBe(6)
    h.destroy()
  })
})
