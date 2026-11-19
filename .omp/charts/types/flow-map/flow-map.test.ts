import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import flowMap, { collectFlows } from './index'

chartSuite({
  module: flowMap,
  dir: import.meta.dirname,
  role: 'flow',
  marks: {
    basic: 8, realistic: 20, 'us-states': 17, 'edge-single': 1, 'edge-zeros': 8, 'edge-negatives': 4, 'edge-nulls': 2,
    'edge-duplicates': 3, 'edge-large': 5200, 'edge-long-labels': 4, 'edge-unicode-rtl': 6, 'edge-outliers': 5, 'edge-unmatched': 2, 'edge-lonlat': 9,
  },
  summary: /^Flow map, 8 flows between 9 places, largest Brazil → United States of America at 480, total 1,880$/,
  malformed: [
    { name: 'a text value', data: [{ from: 'BRA', to: 'USA', value: 1 }, { from: 'BRA', to: 'DEU', value: 'lots' }], message: /field 'value' must be a number, got 'lots' at row 2/ },
    { name: 'a text coordinate', data: [{ fromLon: 'west', fromLat: 1, toLon: 2, toLat: 3, value: 1 }], message: /field 'fromLon' must be a number, got 'west' at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const widths = (el: HTMLElement) => new Map([...el.querySelectorAll('[data-role="flow"]')].map((p) => [p.getAttribute('data-key'), Number(p.getAttribute('stroke-width') ?? (p as SVGElement).style.strokeWidth)]))

describe('flow-map', () => {
  it('stroke width is proportional to the value above the minimum', () => {
    const el = host(800, 450)
    const h = flowMap.render(el, [{ from: 'BRA', to: 'USA', value: 100 }, { from: 'DEU', to: 'IND', value: 50 }, { from: 'JPN', to: 'AUS', value: 0 }], { animate: false, maxWidth: 11 })
    const w = widths(el)
    expect(w.get('path:flow:BRA>USA')).toBeCloseTo(11, 5)
    expect(w.get('path:flow:DEU>IND')).toBeCloseTo(6, 5)
    expect(w.get('path:flow:JPN>AUS')).toBeCloseTo(1, 5)
    h.destroy()
  })
  it('one arrowhead per flow, at the destination', () => {
    const el = host(800, 450)
    const h = flowMap.render(el, [{ from: 'BRA', to: 'USA', value: 100 }, { from: 'USA', to: 'BRA', value: 30 }], { animate: false })
    const arrows = [...el.querySelectorAll('[data-key^="path:arrow:"]')]
    expect(arrows).toHaveLength(2)
    // A->B and B->A bow to opposite sides: their midpoints differ
    const flows = [...el.querySelectorAll('[data-role="flow"]')].map((p) => p.getAttribute('d'))
    expect(flows[0]).not.toBe(flows[1])
    h.destroy()
  })
  it('great-circle A->B and B->A are drawn apart, so both can be seen and hovered', () => {
    const el = host(800, 450)
    const h = flowMap.render(el, [{ from: 'RUS', to: 'UKR', value: 3 }, { from: 'UKR', to: 'RUS', value: 3 }], { animate: false, arc: 'great-circle' })
    const hits = [...el.querySelectorAll('[data-role="flow"][data-mark]')].map((p) => p.getAttribute('d')!)
    expect(hits).toHaveLength(2)
    // the reverse path, walked backwards, would coincide without the offset
    const start = (d: string) => d.match(/M(-?[\d.]+),(-?[\d.]+)/)!.slice(1).map(Number)
    const ends = (d: string) => [...d.matchAll(/[ML](-?[\d.]+),(-?[\d.]+)/g)].map((m) => [Number(m[1]), Number(m[2])])
    const a = ends(hits[0]!), b = ends(hits[1]!)
    const gap = Math.hypot(a[Math.floor(a.length / 2)]![0]! - b[Math.floor(b.length / 2)]![0]!, a[Math.floor(a.length / 2)]![1]! - b[Math.floor(b.length / 2)]![1]!)
    expect(gap).toBeGreaterThanOrEqual(7)
    expect(start(hits[0]!)).not.toEqual(start(hits[1]!))
    h.destroy()
  })
  it('great-circle arcs split at the antimeridian instead of crossing the map', () => {
    const el = host(800, 450)
    const h = flowMap.render(el, [{ fromLon: 139.78, fromLat: 35.55, toLon: -157.92, toLat: 21.32, value: 1 }], { animate: false, arc: 'great-circle' })
    const d = el.querySelector('[data-role="flow"]')!.getAttribute('d')!
    expect(d.match(/M/g)).toHaveLength(2)
    const xs = [...d.matchAll(/[ML](-?[\d.]+),/g)].map((m) => Number(m[1]))
    for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i]! - xs[i - 1]!) > 200) expect(d.indexOf('M', 1)).toBeGreaterThan(0)
    h.destroy()
  })
  it('accepts region keys in any spelling and notes unmatched ones', () => {
    const c = collectFlows([{ from: 'br', to: 'United States', fromLon: null, fromLat: null, toLon: null, toLat: null, value: 1 }, { from: 'Atlantis', to: 'USA', fromLon: null, fromLat: null, toLon: null, toLat: null, value: 1 }], { map: 'world', arc: 'curved', colorBy: 'single', showLabels: true })
    expect(c.flows.map((f) => f.key)).toEqual(['BRA>USA'])
    expect(c.unmatched).toEqual(['Atlantis'])
    const el = host()
    const h = flowMap.render(el, [{ from: 'BRA', to: 'USA', value: 1 }, { from: 'Atlantis', to: 'USA', value: 1 }], { animate: false })
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 region not found: Atlantis')
    h.destroy()
  })
  it('colour by origin: legend toggles an origin and its arrowheads', () => {
    const el = host()
    const h = flowMap.render(el, [{ from: 'BRA', to: 'USA', value: 1 }, { from: 'DEU', to: 'USA', value: 1 }], { animate: false, colorBy: 'origin' })
    const buttons = [...el.querySelectorAll('.oc-legend button')] as HTMLButtonElement[]
    expect(buttons.map((b) => b.textContent)).toEqual(['Brazil', 'Germany'])
    buttons[0]!.click()
    expect(h.markCount()).toBe(1)
    expect(el.querySelectorAll('[data-key^="path:arrow:"]')).toHaveLength(1)
    h.destroy()
  })
  it('flows and arrowheads stay inside the box', () => {
    const el = host(500, 280)
    const h = flowMap.render(el, [{ from: 'NZL', to: 'ISL', value: 5 }, { from: 'CHL', to: 'RUS', value: 3 }], { animate: false })
    const svg = el.querySelector('svg.oc-svg')!
    const W = Number(svg.getAttribute('width')), H = Number(svg.getAttribute('height'))
    for (const p of el.querySelectorAll('[data-role="flow"], [data-key^="path:arrow:"]')) {
      const nums = p.getAttribute('d')!.match(/-?\d+(\.\d+)?/g)!.map(Number)
      nums.forEach((n, i) => { expect(n).toBeGreaterThanOrEqual(-1); expect(n).toBeLessThanOrEqual((i % 2 ? H : W) + 1) })
    }
    h.destroy()
  })
})
