import { describe, expect, it } from 'vitest'
import { chartSuite, host, loadSamples } from '../../tests/harness'
import bar from '../bar'
import line from '../line'
import smallMultiples, { facetGroups, sharedY } from './index'
import { options as optionSchema } from './schema'

chartSuite({
  module: smallMultiples,
  dir: import.meta.dirname,
  role: 'bar',
  marks: { basic: 16, 'edge-single': 1, 'edge-zeros': 6, 'edge-negatives': 6, 'edge-nulls': 3, 'edge-duplicates': 2, 'edge-large': 5200, 'edge-outliers': 9 },
  summary: /^Small multiples, 4 panels of bar chart by region \(North, South, East and West\), shared y scale 0 to 340$/,
  malformed: [
    { name: 'a missing facet column', data: [{ label: 'a', value: 1 }], options: { facet: 'region' }, message: /facet column 'region' not found \(columns: label, value\)/ },
    { name: 'an unknown child chart', data: [{ region: 'N', label: 'a', value: 1 }], options: { facet: 'region', chart: 'nope' }, message: /unknown chart 'nope'/ },
    { name: 'itself as the child', data: [{ region: 'N' }], options: { facet: 'region', chart: 'small-multiples' }, message: /cannot contain small multiples/ },
    { name: 'a scalar', data: 42, options: { facet: 'region' }, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const basic = loadSamples(import.meta.dirname).find((s) => s.name === 'basic')!

describe('small-multiples: facets and shared scales', () => {
  it('4 facets -> 4 titles, and the child mark counts add up', () => {
    const el = host(800, 600)
    const h = smallMultiples.render(el, basic.data, { ...basic.options, animate: false })
    const titles = [...el.querySelectorAll('[data-key^="text:title:"]')].map((t) => t.textContent)
    expect(titles).toEqual(['North', 'South', 'East', 'West'])
    let sum = 0
    for (const g of facetGroups(basic.data as Array<Record<string, unknown>>, 'region')) {
      const one = host(300, 200)
      const c = bar.render(one, g.rows, { animate: false })
      sum += c.markCount()
      c.destroy()
    }
    expect(h.markCount()).toBe(sum)
    expect(h.markCount()).toBe(16)
    h.destroy()
  })
  it('shares the y domain: identical y tick labels in every panel', () => {
    const el = host(800, 600)
    const h = smallMultiples.render(el, basic.data, { ...basic.options, animate: false })
    const ticks = (facet: string) => [...el.querySelectorAll(`[data-key^="text:f:${facet}:yl:"]`)].map((t) => t.textContent)
    const north = ticks('North')
    expect(north.length).toBeGreaterThan(2)
    for (const f of ['South', 'East', 'West']) expect(ticks(f)).toEqual(north)
    const o = optionSchema.parse(basic.options)
    expect(sharedY(basic.data as Array<Record<string, unknown>>, o)).toEqual([0, 340])
    h.destroy()
  })
  it("sharedScale 'none' lets each panel scale to its own data", () => {
    const el = host(800, 400)
    const data = [{ g: 'a', label: 'x', value: 1 }, { g: 'b', label: 'x', value: 1000 }]
    const h = smallMultiples.render(el, data, { facet: 'g', sharedScale: 'none', animate: false })
    const top = (f: string) => [...el.querySelectorAll(`[data-key^="text:f:${f}:yl:"]`)].map((t) => t.textContent).pop()
    expect(top('a')).not.toEqual(top('b'))
    h.destroy()
  })
  it('columns fixes the panels per row', () => {
    const el = host(900, 600)
    const h = smallMultiples.render(el, basic.data, { ...basic.options, columns: 1, animate: false })
    const ys = [...el.querySelectorAll('[data-key^="text:title:"]')].map((t) => Number(t.getAttribute('y')))
    expect(new Set(ys).size).toBe(4)
    h.destroy()
  })
})

describe('small-multiples: with line', () => {
  const rows = ['A', 'B', 'C', 'D'].flatMap((g, gi) =>
    Array.from({ length: 6 }, (_, i) => ({ node: g, x: `2026-0${i + 1}-01`, y: (gi + 1) * 10 + i * (gi % 2 ? 3 : -1) })))
  it('4 line panels: 4 titles, child point counts add up, identical y ticks', () => {
    const el = host(800, 600)
    const h = smallMultiples.render(el, rows, { chart: 'line', facet: 'node', animate: false })
    expect([...el.querySelectorAll('[data-key^="text:title:"]')].map((t) => t.textContent)).toEqual(['A', 'B', 'C', 'D'])
    let sum = 0
    for (const g of facetGroups(rows, 'node')) {
      const one = host(300, 200)
      const c = line.render(one, g.rows, { animate: false })
      sum += c.markCount()
      c.destroy()
    }
    expect(sum).toBe(24)
    expect(h.markCount()).toBe(sum)
    const ticks = (f: string) => [...el.querySelectorAll(`[data-key^="text:f:${f}:yl:"]`)].map((t) => t.textContent)
    expect(ticks('A').length).toBeGreaterThan(2)
    for (const f of ['B', 'C', 'D']) expect(ticks(f)).toEqual(ticks('A'))
    h.destroy()
  })
  it("sharedScale 'both' also shares the date x domain", () => {
    const uneven = [...rows.filter((r) => r.node === 'A'), { node: 'B', x: '2026-03-01', y: 5 }, { node: 'B', x: '2026-04-01', y: 7 }]
    const el = host(800, 400)
    const h = smallMultiples.render(el, uneven, { chart: 'line', facet: 'node', sharedScale: 'both', animate: false })
    const xt = (f: string) => [...el.querySelectorAll(`[data-key^="text:f:${f}:xl:"]`)].map((t) => t.textContent)
    expect(xt('A').length).toBeGreaterThan(1)
    expect(xt('B')).toEqual(xt('A'))
    h.destroy()
  })
})
