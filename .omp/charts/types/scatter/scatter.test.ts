import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { autoRadius } from '../_shared/RelationshipA'
import scatter from './index'

chartSuite({
  module: scatter,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 10, realistic: 37, 'edge-single': 1, 'edge-duplicates': 20, 'edge-nulls': 5, 'edge-large': 6000, 'edge-zeros': 10 },
  summary: /^Scatter plot, 10 points, Hours studied from 1 to 10, Score from 52 to 88, strong positive correlation \(r = 0\.98\)$/,
  malformed: [
    { name: 'a text value', data: [{ x: 1, y: 2 }, { x: 'abc', y: 3 }], message: /field 'x' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ a: 1, b: 2 }], options: { fields: { x: 'a', y: 'bb' } }, message: /column 'bb' not found/ },
  ],
})

const pos = (el: Element) => [...el.querySelectorAll('[data-role="point"]')].map((c) => [Number(c.getAttribute('cx')), Number(c.getAttribute('cy'))] as const)

describe('scatter: geometry', () => {
  it('places points linearly on both axes', () => {
    const el = host(640, 400)
    const h = scatter.render(el, [{ x: 0, y: 0 }, { x: 10, y: 10 }, { x: 5, y: 2.5 }], { animate: false })
    const [a, b, c] = pos(el)
    expect((c![0] - a![0]) / (b![0] - a![0])).toBeCloseTo(0.5, 2)
    expect((a![1] - c![1]) / (a![1] - b![1])).toBeCloseTo(0.25, 2)
    h.destroy()
  })
  it('log scale spaces decades equally and omits values <= 0 with a note', () => {
    const el = host(640, 400)
    const h = scatter.render(el, [{ x: 1, y: 1 }, { x: 10, y: 2 }, { x: 100, y: 3 }, { x: 0, y: 4 }, { x: -5, y: 5 }], { animate: false, xScale: 'log' })
    expect(h.markCount()).toBe(3)
    const [a, b, c] = pos(el)
    expect(b![0] - a![0]).toBeCloseTo(c![0] - b![0], 1)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/2 values ≤ 0 omitted on the log scale/)
    h.destroy()
  })
  it('one legend entry per series; toggling hides that series', () => {
    const el = host(640, 400)
    const h = scatter.render(el, [{ x: 1, y: 1, series: 'a' }, { x: 2, y: 2, series: 'b' }, { x: 3, y: 1, series: 'a' }], { animate: false })
    const buttons = [...el.querySelectorAll('.oc-legend button')]
    expect(buttons.map((b) => b.textContent)).toEqual(['a', 'b'])
    ;(buttons[0] as HTMLButtonElement).click()
    expect(h.markCount()).toBe(1)
    h.destroy()
  })
  it('notes skipped rows and overlapping points', () => {
    const el = host()
    const h = scatter.render(el, [{ x: 1, y: 1 }, { x: 1, y: 1 }, { x: null, y: 2 }], { animate: false })
    expect(h.markCount()).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 rows missing x or y skipped; 1 points overlap another at the same position')
    h.destroy()
  })
  it('direct labels from the label field stay inside the plot', () => {
    const el = host(640, 400)
    const h = scatter.render(el, [{ x: 1, y: 1, label: 'Alpha' }, { x: 9, y: 9, label: 'Omega' }], { animate: false })
    const texts = [...el.querySelectorAll('.oc-labels text')].map((t) => t.textContent)
    expect(texts).toEqual(['Alpha', 'Omega'])
    h.destroy()
  })
  it('shrinks dots as the count grows and switches to canvas above 5k', () => {
    expect(autoRadius(10)).toBeGreaterThan(autoRadius(500))
    expect(autoRadius(500)).toBeGreaterThan(autoRadius(5000))
    const el = host()
    const data = Array.from({ length: 5200 }, (_, i) => ({ x: i % 97, y: (i * 7) % 89 }))
    const h = scatter.render(el, data, { animate: false })
    expect(h.renderer).toBe('canvas')
    expect(h.markCount()).toBe(5200)
    h.destroy()
  })
  it('zoom (wheel) rescales both axes and drops points outside the plot', () => {
    const el = host(640, 400)
    const data = Array.from({ length: 21 }, (_, i) => ({ x: i, y: i }))
    const zooms: number[] = []
    const h = scatter.render(el, data, { animate: false, zoom: true, onZoom: (z: { k: number }) => zooms.push(z.k) })
    expect(h.markCount()).toBe(21)
    const svg = el.querySelector('svg.oc-svg')!
    svg.dispatchEvent(new WheelEvent('wheel', { deltaY: -600, deltaMode: 0, clientX: 320, clientY: 200, bubbles: true, cancelable: true }))
    // d3-zoom applies a wheel step synchronously
    expect(zooms.length).toBeGreaterThan(0)
    expect(h.markCount()).toBeLessThan(21)
    expect(h.markCount()).toBeGreaterThan(0)
    h.destroy()
  })
})
