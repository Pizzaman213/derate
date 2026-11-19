import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import multiLine from './index'

chartSuite({
  module: multiLine,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 48, realistic: 260, 'edge-single': 1, 'edge-duplicates': 48, 'edge-large': 5040 },
  summary: /^Multi-line chart, 4 series, Jan 2026 to Dec 2026, highest North at 66k in Dec 2026$/,
  malformed: [
    { name: 'a text value', data: [{ x: '2026-01-01', series: 'a', y: 1 }, { x: '2026-02-01', series: 'a', y: 'abc' }], message: /field 'y' must be a number, got 'abc' at row 2/ },
    { name: 'a bad date', data: [{ x: 'nope', series: 'a', y: 1 }], message: /field 'x' must be a date, got 'nope' at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const data = [
  { x: '2026-01-01', series: 'a', y: 1 }, { x: '2026-02-01', series: 'a', y: 2 },
  { x: '2026-01-01', series: 'b', y: 5 }, { x: '2026-02-01', series: 'b', y: 6 },
]

describe('multi-line: series', () => {
  it('draws one line per series with a legend and end labels', () => {
    const el = host(800, 400)
    const h = multiLine.render(el, data, { animate: false })
    expect(el.querySelectorAll('[data-key^="path:line:"]').length).toBe(2)
    expect(el.querySelectorAll('[data-key^="text:end:"]').length).toBe(2)
    expect(el.querySelectorAll('.oc-legend [data-series], .oc-legend button').length).toBeGreaterThanOrEqual(2)
    h.destroy()
  })
  it('legend toggle hides a series and rescales y', () => {
    const el = host(800, 400)
    const h = multiLine.render(el, data, { animate: false })
    const yTicks = () => [...el.querySelectorAll('[data-key^="text:yl:"]')].map((t) => t.textContent)
    const before = yTicks()
    const btn = [...el.querySelectorAll<HTMLElement>('.oc-legend button, .oc-legend [data-series]')].find((b) => b.textContent?.includes('b'))!
    btn.click()
    expect(el.querySelectorAll('[data-key^="path:line:"]').length).toBe(1)
    expect(h.markCount()).toBe(2)
    expect(yTicks()).not.toEqual(before)
    h.destroy()
  })
  it('drops end labels on narrow charts', () => {
    const el = host(320, 300)
    const h = multiLine.render(el, data, { animate: false })
    expect(el.querySelectorAll('[data-key^="text:end:"]').length).toBe(0)
    h.destroy()
  })
  it('a single named series is named in the tooltip, not "Value"', () => {
    const el = host(800, 400)
    const h = multiLine.render(el, data.filter((r) => r.series === 'b'), { animate: false, points: 'never' })
    const dot = el.querySelector<SVGCircleElement>('[data-role="point"]')!
    dot.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientX: Number(dot.getAttribute('cx')), clientY: Number(dot.getAttribute('cy')) }))
    const rows = [...el.querySelectorAll('.oc-tooltip tr')].map((r) => r.textContent)
    expect(rows).toEqual(['b5'])
    h.destroy()
  })
  it('hovering anywhere over the plot names every series at the nearest x', () => {
    const el = host(800, 400)
    const h = multiLine.render(el, data, { animate: false, points: 'never' })
    const svg = el.querySelector('svg')!
    const dots = [...el.querySelectorAll<SVGCircleElement>('[data-role="point"]')]
    const cx = Math.max(...dots.map((d) => Number(d.getAttribute('cx'))))
    const cy = (Math.min(...dots.map((d) => Number(d.getAttribute('cy')))) + Math.max(...dots.map((d) => Number(d.getAttribute('cy'))))) / 2
    svg.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientX: cx - 3, clientY: cy }))
    const tip = el.querySelector<HTMLElement>('.oc-tooltip')!
    expect(tip.hidden).toBe(false)
    const rows = [...tip.querySelectorAll('tr')].map((r) => r.textContent)
    expect(rows).toEqual(['a2', 'b6'])
    svg.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientX: 2, clientY: 2 }))
    expect(tip.hidden).toBe(true)
    h.destroy()
  })
})
