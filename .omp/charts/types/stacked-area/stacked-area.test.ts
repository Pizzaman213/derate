import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import stackedArea from './index'

chartSuite({
  module: stackedArea,
  dir: import.meta.dirname,
  role: 'point',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 48, realistic: 130, 'edge-single': 1, 'edge-duplicates': 48, 'edge-large': 5200 },
  summary: /^Stacked area chart, 4 series, Jan 2026 to Dec 2026, total peaks at 195k in Dec 2026, largest North$/,
  malformed: [
    { name: 'a text value', data: [{ x: '2026-01-01', series: 'a', y: 'lots' }], message: /field 'y' must be a number, got 'lots' at row 1/ },
    { name: 'a bad date', data: [{ x: 'soon', series: 'a', y: 1 }], message: /field 'x' must be a date, got 'soon' at row 1/ },
    { name: 'a scalar', data: true, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const stackData = [
  { x: '2026-01-01', series: 'a', y: 10 }, { x: '2026-01-01', series: 'b', y: 30 }, { x: '2026-01-01', series: 'c', y: 60 },
  { x: '2026-02-01', series: 'a', y: 20 }, { x: '2026-02-01', series: 'b', y: 20 }, { x: '2026-02-01', series: 'c', y: 10 },
]
type D = { x: Date; series: string; y: number; y0: number; y1: number; total: number }
const clicked = (data: unknown, opts: Record<string, unknown> = {}) => {
  const el = host(800, 400)
  const got: D[] = []
  const h = stackedArea.render(el, data, { animate: false, ...opts, onClick: (d: unknown) => { got.push(d as D) } })
  for (const c of el.querySelectorAll('[data-role="point"]')) c.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  return { el, h, got }
}

describe('stacked-area: stacking', () => {
  it('draws one layer per series and one point per (x, series)', () => {
    const el = host(800, 400)
    const h = stackedArea.render(el, stackData, { animate: false })
    expect(el.querySelectorAll('[data-key^="path:area:"]').length).toBe(3)
    expect(h.markCount()).toBe(6)
    h.destroy()
  })
  it('legend toggle restacks without the hidden series', () => {
    const el = host(800, 400)
    const h = stackedArea.render(el, stackData, { animate: false })
    const before = el.querySelector('[data-key="path:area:c"]')!.getAttribute('d')
    const btn = [...el.querySelectorAll<HTMLElement>('.oc-legend button, .oc-legend [data-series]')].find((b) => b.textContent?.trim().endsWith('a'))!
    btn.click()
    expect(el.querySelectorAll('[data-key^="path:area:"]').length).toBe(2)
    expect(el.querySelector('[data-key="path:area:c"]')!.getAttribute('d')).not.toBe(before)
    h.destroy()
  })
  it('stacks: each layer starts where the one below ends, and the top is the total', () => {
    const { h, got } = clicked(stackData)
    expect(got.length).toBe(6)
    for (const t of ['2026-01-01', '2026-02-01']) {
      const at = got.filter((d) => d.x.toISOString().startsWith(t)).sort((a, b) => a.y0 - b.y0)
      expect(at[0]!.y0).toBe(0)
      for (let i = 1; i < at.length; i++) expect(at[i]!.y0).toBeCloseTo(at[i - 1]!.y1, 9)
      for (const d of at) expect(d.y1 - d.y0).toBeCloseTo(d.y, 9)
      expect(at[at.length - 1]!.y1).toBeCloseTo(at.reduce((a, d) => a + d.y, 0), 9)
    }
    h.destroy()
  })
  it('draws negatives as zero and says so', () => {
    const { el, h } = clicked([...stackData, { x: '2026-03-01', series: 'a', y: -5 }, { x: '2026-03-01', series: 'b', y: 5 }])
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 negative values drawn as zero/)
    h.destroy()
  })
})
