import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { findCountry, findState, resolveRegion, worldCountries, usStates } from '../../geo'
import { fitMap } from '../_shared/GeoCharts'
import choropleth from './index'

chartSuite({
  module: choropleth,
  dir: import.meta.dirname,
  role: 'region',
  marks: {
    basic: 12, realistic: 58, 'us-states': 50 + 1, 'edge-single': 1, 'edge-zeros': 12, 'edge-negatives': 14, 'edge-nulls': 4,
    'edge-duplicates': 4, 'edge-large': 177, 'edge-long-labels': 7, 'edge-unicode-rtl': 11, 'edge-outliers': 13, 'edge-unmatched': 3,
  },
  summary: /^Choropleth map, 12 countries by Internet users \(%\), highest Australia at 96\.2, lowest India at 46\.3$/,
  malformed: [
    { name: 'a text value', data: [{ region: 'BRA', value: 1 }, { region: 'USA', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ country: 'BRA', v: 1 }], options: { fields: { region: 'country', value: 'val' } }, message: /column 'val' not found/ },
  ],
})

describe('geo lookup', () => {
  it('resolves countries by ISO3, ISO2, numeric and name, case- and accent-insensitively', () => {
    for (const k of ['FRA', 'fra', 'FR', 'fr', '250', 'France', 'france', ' FRANCE ', 'French Republic']) expect(findCountry(k)?.iso3, k).toBe('FRA')
    expect(findCountry('4')?.iso3).toBe('AFG')
    expect(findCountry('cote divoire')?.iso3).toBe('CIV')
    expect(findCountry("Côte d'Ivoire")?.iso3).toBe('CIV')
    expect(findCountry('United States')?.iso3).toBe('USA')
    expect(findCountry('Dem. Rep. Congo')?.iso3).toBe('COD')
    expect(findCountry('Atlantis')).toBeNull()
  })
  it('resolves states by FIPS, postal code and name', () => {
    for (const k of ['06', '6', 'CA', 'ca', 'California', 'CALIFORNIA']) expect(findState(k)?.postal, k).toBe('CA')
    expect(findState('District of Columbia')?.fips).toBe('11')
    expect(findState('Georgia')?.postal).toBe('GA')
    expect(findState('Narnia')).toBeNull()
  })
  it('every topology feature has a canonical key and every table row a feature', () => {
    const world = worldCountries()
    expect(world).toHaveLength(177)
    expect(new Set(world.map((f) => f.properties.key)).size).toBe(177)
    expect(resolveRegion('world', 'GEO')?.name).toBe('Georgia')
    expect(resolveRegion('us-states', 'Georgia')?.key).toBe('GA')
    expect(usStates().filter((f) => /^[A-Z]{2}$/.test(f.properties.key))).toHaveLength(56)
    expect(worldCountries()).toBe(world) // memoised
  })
})

describe('choropleth: geometry', () => {
  const inside = (el: HTMLElement, w: number, h: number) => {
    const ds = [...el.querySelectorAll('svg.oc-svg path')].map((p) => p.getAttribute('d') ?? '')
    const nums = ds.join(' ').match(/-?\d+(\.\d+)?/g)!.map(Number)
    const xs = nums.filter((_, i) => i % 2 === 0), ys = nums.filter((_, i) => i % 2 === 1)
    expect(Math.min(...xs)).toBeGreaterThanOrEqual(-0.5)
    expect(Math.max(...xs)).toBeLessThanOrEqual(w + 0.5)
    expect(Math.min(...ys)).toBeGreaterThanOrEqual(-0.5)
    expect(Math.max(...ys)).toBeLessThanOrEqual(h + 0.5)
  }
  it('the world projection fits inside the box', () => {
    for (const [w, h] of [[640, 360], [320, 400], [1200, 300]] as const) {
      const f = fitMap('world', w, h)
      const [[x0, y0], [x1, y1]] = f.path.bounds({ type: 'Sphere' })
      expect(x0).toBeGreaterThanOrEqual(1.5)
      expect(y0).toBeGreaterThanOrEqual(1.5)
      expect(x1).toBeLessThanOrEqual(w - 1.5)
      expect(y1).toBeLessThanOrEqual(h - 1.5)
      // fitted: touches the box on one axis
      expect(Math.max((x1 - x0) / (w - 4), (y1 - y0) / (h - 4))).toBeCloseTo(1, 2)
    }
  })
  it('the US projection fits inside the box', () => {
    const f = fitMap('us-states', 500, 300)
    const [[x0, y0], [x1, y1]] = f.path.bounds({ type: 'FeatureCollection', features: usStates() })
    expect(x0).toBeGreaterThanOrEqual(1.5)
    expect(y0).toBeGreaterThanOrEqual(1.5)
    expect(x1).toBeLessThanOrEqual(498.5)
    expect(y1).toBeLessThanOrEqual(298.5)
  })
  it('rendered paths stay inside the svg (world and US)', () => {
    for (const map of ['world', 'us-states'] as const) {
      const el = host(600, 340)
      const h = choropleth.render(el, [{ region: map === 'world' ? 'BRA' : 'TX', value: 1 }], { animate: false, map })
      const svg = el.querySelector('svg.oc-svg')!
      inside(el, Number(svg.getAttribute('width')), Number(svg.getAttribute('height')))
      h.destroy()
    }
  })
  it('matches ISO3, ISO2, numeric and names, and notes unmatched keys', () => {
    const el = host()
    const h = choropleth.render(el, [{ region: 'BRA', value: 1 }, { region: 'fr', value: 2 }, { region: '356', value: 3 }, { region: 'Japan', value: 4 }, { region: 'Atlantis', value: 5 }, { region: 'Narnia', value: 6 }], { animate: false })
    expect(h.markCount()).toBe(4)
    expect([...el.querySelectorAll('[data-role="region"]')].map((p) => p.getAttribute('data-key'))).toEqual(['path:region:BRA', 'path:region:FRA', 'path:region:IND', 'path:region:JPN'])
    expect(el.querySelector('.oc-note')!.textContent).toBe('2 regions not found: Atlantis, Narnia')
    h.destroy()
  })
  it('matches US states by FIPS, postal code and name', () => {
    const el = host()
    const h = choropleth.render(el, [{ region: '06', value: 1 }, { region: 'tx', value: 2 }, { region: 'New York', value: 3 }, { region: 'XX', value: 4 }], { animate: false, map: 'us-states' })
    expect(h.markCount()).toBe(3)
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 region not found: XX')
    h.destroy()
  })
  it('regions without data are drawn in the track colour and are not data marks', () => {
    const el = host()
    const h = choropleth.render(el, [{ region: 'BRA', value: 1 }], { animate: false })
    const base = [...el.querySelectorAll('svg.oc-svg path:not([data-mark])')]
    expect(base.length).toBeGreaterThanOrEqual(176)
    h.destroy()
  })
  it('a diverging scale when values span zero, sequential otherwise', () => {
    const el = host()
    const h = choropleth.render(el, [{ region: 'BRA', value: -2 }, { region: 'USA', value: 4 }], { animate: false })
    const bar = () => (el.querySelector('.oc-gradient .oc-bar') as HTMLElement).style.background
    expect(el.querySelector('.oc-gradient')!.textContent).toMatch(/^[-−]4.*4$/)
    expect(bar()).toMatch(/--oc-div-/)
    h.update([{ region: 'BRA', value: 2 }, { region: 'USA', value: 4 }], {})
    expect(el.querySelector('.oc-gradient')!.textContent).toBe('24')
    expect(bar()).toMatch(/--oc-seq-/)
    h.destroy()
  })
  it('sums duplicate spellings of one region', () => {
    const el = host()
    const h = choropleth.render(el, [{ region: 'USA', value: 1 }, { region: 'United States', value: 2 }], { animate: false })
    expect(h.markCount()).toBe(1)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate regions summed/)
    h.focusMark(0)
    expect(el.querySelector('[aria-live]')!.textContent).toBe('1 of 1: United States of America: 3')
    h.destroy()
  })
})
