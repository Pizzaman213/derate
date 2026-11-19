import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import dotMap, { categoriesOf } from './index'

chartSuite({
  module: dotMap,
  dir: import.meta.dirname,
  role: 'dot',
  marks: {
    basic: 20, realistic: 633, 'edge-single': 1, 'edge-zeros': 10, 'edge-nulls': 3, 'edge-duplicates': 12, 'edge-negatives': 9,
    'edge-large': 5100, 'edge-long-labels': 5, 'edge-unicode-rtl': 8, 'edge-outliers': 34, 'edge-many-categories': 160,
  },
  summary: /^Dot map, 20 locations in 5 categories \(Asia, Africa, Europe and 2 more\), from -36\.9° to 52\.5° latitude and -99\.1° to 174\.8° longitude$/,
  malformed: [
    { name: 'a text latitude', data: [{ lon: 1, lat: 2 }, { lon: 3, lat: 'north' }], message: /field 'lat' must be a number, got 'north' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ x: 1, y: 2 }], options: { fields: { lon: 'x', lat: 'yy' } }, message: /column 'yy' not found/ },
  ],
})

describe('dot-map', () => {
  it('one dot per valid row, inside the box', () => {
    const el = host(500, 280)
    const h = dotMap.render(el, [{ lon: -179.9, lat: 89 }, { lon: 179.9, lat: -89 }, { lon: 0, lat: 0 }], { animate: false })
    expect(h.markCount()).toBe(3)
    const svg = el.querySelector('svg.oc-svg')!
    const W = Number(svg.getAttribute('width')), H = Number(svg.getAttribute('height'))
    for (const c of el.querySelectorAll('[data-role="dot"]')) {
      expect(Number(c.getAttribute('cx'))).toBeGreaterThanOrEqual(0)
      expect(Number(c.getAttribute('cx'))).toBeLessThanOrEqual(W)
      expect(Number(c.getAttribute('cy'))).toBeGreaterThanOrEqual(0)
      expect(Number(c.getAttribute('cy'))).toBeLessThanOrEqual(H)
    }
    h.destroy()
  })
  it('notes rows off the US projection and rows without coordinates', () => {
    const el = host()
    const h = dotMap.render(el, [{ lon: -100, lat: 40 }, { lon: 2.35, lat: 48.86 }, { lon: null, lat: 3 }], { animate: false, map: 'us-states' })
    expect(h.markCount()).toBe(1)
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 row without a valid lon/lat skipped; 1 outside the US map')
    h.destroy()
  })
  it('legend by category; hiding one drops its dots', () => {
    const el = host()
    const data = [{ lon: 1, lat: 1, category: 'a' }, { lon: 2, lat: 2, category: 'b' }, { lon: 3, lat: 3, category: 'b' }]
    const h = dotMap.render(el, data, { animate: false })
    const buttons = [...el.querySelectorAll('.oc-legend button')]
    expect(buttons.map((b) => b.textContent)).toEqual(['a', 'b'])
    ;(buttons[1] as HTMLButtonElement).click()
    expect(h.markCount()).toBe(1)
    h.destroy()
  })
  it('merges the tail of many categories into Other', () => {
    const rows = 'abcdefghij'.split('').flatMap((c, i) => Array.from({ length: 10 - i }, () => ({ lon: 0, lat: 0, category: c })))
    const c = categoriesOf(rows, 4)
    expect(c.names).toEqual(['a', 'b', 'c', 'Other'])
    expect(c.merged).toBe(7)
    expect(c.of('j')).toBe('Other')
  })
  it('switches to canvas above 5,000 dots', () => {
    const el = host()
    const data = Array.from({ length: 5100 }, (_, i) => ({ lon: (i % 360) - 180, lat: (i % 160) - 80 }))
    const h = dotMap.render(el, data, { animate: false })
    expect(h.renderer).toBe('canvas')
    expect(h.markCount()).toBe(5100)
    h.destroy()
  })
})
