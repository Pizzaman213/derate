import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import bubbleMap from './index'

chartSuite({
  module: bubbleMap,
  dir: import.meta.dirname,
  role: 'bubble',
  marks: {
    basic: 10, realistic: 36, 'us-cities': 18, 'edge-single': 1, 'edge-zeros': 10, 'edge-negatives': 6, 'edge-nulls': 3,
    'edge-duplicates': 4, 'edge-large': 5200, 'edge-long-labels': 5, 'edge-unicode-rtl': 8, 'edge-outliers': 8, 'edge-unmatched': 2,
  },
  summary: /^Bubble map, 10 bubbles sized by Population \(millions\), highest India at 1,430, lowest Mexico at 128$/,
  malformed: [
    { name: 'a text value', data: [{ region: 'BRA', value: 1 }, { region: 'USA', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a text longitude', data: [{ lon: 'east', lat: 1, value: 1 }], message: /field 'lon' must be a number, got 'east' at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const radii = (el: HTMLElement) => new Map([...el.querySelectorAll('[data-role="bubble"]')].map((c) => [c.getAttribute('data-key'), Number(c.getAttribute('r'))]))

describe('bubble-map: geometry', () => {
  it('bubble area is proportional to the value (radius ~ sqrt)', () => {
    const el = host(800, 450)
    const h = bubbleMap.render(el, [{ region: 'USA', value: 400 }, { region: 'BRA', value: 100 }, { region: 'IND', value: 25 }], { animate: false })
    const r = radii(el)
    expect(r.get('circle:r:USA')! / r.get('circle:r:BRA')!).toBeCloseTo(2, 2)
    expect(r.get('circle:r:USA')! / r.get('circle:r:IND')!).toBeCloseTo(4, 1)
    h.destroy()
  })
  it('places regions at the centroid of their largest polygon', () => {
    const el = host(800, 450)
    const h = bubbleMap.render(el, [{ region: 'USA', value: 1 }, { region: 'FRA', value: 1 }, { lon: -98, lat: 39, value: 1, label: 'Kansas' }], { animate: false })
    const pos = (k: string) => { const c = el.querySelector(`[data-key="${k}"]`)!; return [Number(c.getAttribute('cx')), Number(c.getAttribute('cy'))] }
    const usa = pos('circle:r:USA'), kansas = pos('circle:p:2')
    // the contiguous-US centroid is in Kansas, not pulled toward Alaska
    expect(Math.hypot(usa[0]! - kansas[0]!, usa[1]! - kansas[1]!)).toBeLessThan(25)
    h.destroy()
  })
  it('every bubble is inside the box', () => {
    const el = host(400, 240)
    const h = bubbleMap.render(el, [{ lon: -179.9, lat: 85, value: 10 }, { lon: 179.9, lat: -85, value: 10 }, { region: 'NZL', value: 1 }], { animate: false })
    const svg = el.querySelector('svg.oc-svg')!
    const W = Number(svg.getAttribute('width')), H = Number(svg.getAttribute('height'))
    for (const c of el.querySelectorAll('[data-role="bubble"]')) {
      const cx = Number(c.getAttribute('cx')), cy = Number(c.getAttribute('cy'))
      expect(cx).toBeGreaterThanOrEqual(0)
      expect(cx).toBeLessThanOrEqual(W)
      expect(cy).toBeGreaterThanOrEqual(0)
      expect(cy).toBeLessThanOrEqual(H)
    }
    h.destroy()
  })
  it('notes unmatched regions, off-map points, negatives and missing locations', () => {
    const el = host()
    const h = bubbleMap.render(el, [{ region: 'TX', value: 1 }, { region: 'Narnia', value: 1 }, { lon: 2.35, lat: 48.86, value: 1, label: 'Paris' }, { region: 'CA', value: -3 }, { value: 4 }], { animate: false, map: 'us-states' })
    expect(h.markCount()).toBe(1)
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 region not found: Narnia; 1 outside this map: Paris; 1 negative values omitted; 1 rows without a region or a valid lon/lat skipped')
    h.destroy()
  })
  it('draws a size key with the largest value', () => {
    const el = host()
    const h = bubbleMap.render(el, [{ region: 'USA', value: 400 }, { region: 'BRA', value: 100 }], { animate: false })
    const texts = [...el.querySelectorAll('svg text')].map((t) => t.textContent)
    expect(texts).toContain('400')
    expect(texts).toContain('100')
    h.destroy()
  })
})
