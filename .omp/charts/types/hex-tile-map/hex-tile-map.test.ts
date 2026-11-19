import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { US_HEX } from '../../geo'
import hexTileMap, { hexLayout } from './index'

chartSuite({
  module: hexTileMap,
  dir: import.meta.dirname,
  role: 'hex',
  marks: {
    basic: 51, realistic: 51, 'edge-single': 1, 'edge-zeros': 51, 'edge-negatives': 51, 'edge-nulls': 26,
    'edge-duplicates': 51, 'edge-large': 51, 'edge-outliers': 51, 'edge-long-labels': 5, 'edge-unicode-rtl': 5,
  },
  summary: /^Hex-tile map, 51 states by Bachelor's degree \(%\), highest District of Columbia at 63\.5, lowest West Virginia at 23\.4$/,
  malformed: [
    { name: 'a text value', data: [{ state: 'CA', value: 1 }, { state: 'TX', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ st: 'CA', v: 1 }], options: { fields: { state: 'st', value: 'val' } }, message: /column 'val' not found/ },
  ],
})

describe('hex-tile-map: layout', () => {
  it('the grid has 51 distinct cells: 50 states and DC', () => {
    expect(Object.keys(US_HEX)).toHaveLength(51)
    expect(new Set(Object.values(US_HEX).map((c) => `${c.col},${c.row}`)).size).toBe(51)
    expect(US_HEX.DC).toBeDefined()
  })
  it('hexes never overlap and fit inside the box', () => {
    for (const [w, h] of [[640, 380], [320, 200], [1280, 400]] as const) {
      const cells = hexLayout(w, h)
      const r = cells[0]!.r
      for (let i = 0; i < cells.length; i++) {
        const a = cells[i]!
        expect(a.cx - (Math.sqrt(3) * r) / 2).toBeGreaterThanOrEqual(1.99)
        expect(a.cx + (Math.sqrt(3) * r) / 2).toBeLessThanOrEqual(w - 1.99)
        expect(a.cy - r).toBeGreaterThanOrEqual(1.99)
        expect(a.cy + r).toBeLessThanOrEqual(h - 1.99)
        for (let j = i + 1; j < cells.length; j++) {
          const b = cells[j]!
          // two hexes of inradius sqrt3/2 r overlap iff their centres are closer than sqrt3 r
          expect(Math.hypot(a.cx - b.cx, a.cy - b.cy)).toBeGreaterThanOrEqual(Math.sqrt(3) * r - 1e-6)
        }
      }
    }
  })
  it('51 hexes with postal labels when every state is present', () => {
    const el = host(800, 480)
    const rows = Object.keys(US_HEX).map((p, i) => ({ state: p, value: i }))
    const h = hexTileMap.render(el, rows, { animate: false })
    expect(el.querySelectorAll('[data-role="hex"]')).toHaveLength(51)
    expect(el.querySelectorAll('[data-role="hex-empty"]')).toHaveLength(0)
    const codes = [...el.querySelectorAll('svg text')].map((t) => t.textContent)
    for (const p of Object.keys(US_HEX)) expect(codes).toContain(p)
    h.destroy()
  })
  it('accepts postal codes, FIPS and names; missing states are grey tiles, not data marks', () => {
    const el = host(800, 480)
    const h = hexTileMap.render(el, [{ state: 'ca', value: 1 }, { state: '48', value: 2 }, { state: 'New York', value: 3 }, { state: 'Narnia', value: 4 }], { animate: false })
    expect(h.markCount()).toBe(3)
    expect(el.querySelectorAll('[data-role="hex-empty"]')).toHaveLength(48)
    expect([...el.querySelectorAll('[data-role="hex"]')].map((p) => p.getAttribute('data-key'))).toEqual(['path:hex:CA', 'path:hex:TX', 'path:hex:NY'])
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 state not found: Narnia')
    h.destroy()
  })
  it('notes territories that have no tile', () => {
    const el = host()
    const h = hexTileMap.render(el, [{ state: 'PR', value: 1 }, { state: 'CA', value: 2 }], { animate: false })
    expect(h.markCount()).toBe(1)
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 not on the hex grid: Puerto Rico')
    h.destroy()
  })
})
