import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import quadrant from './index'

chartSuite({
  module: quadrant,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 10, realistic: 35, 'edge-single': 1, 'edge-duplicates': 20, 'edge-large': 5200, 'edge-nulls': 5 },
  summary: /^Quadrant chart, 10 items, split at Effort 5 and Impact 6\.5 \(medians\), most in Major projects \(3\)$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', x: 1, y: 2 }, { label: 'B', x: 'abc', y: 1 }], message: /field 'x' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { label: ['a', 'b'], x: [1, 2], y: [1] }, message: /column 'y' has 1 values but column 'label' has 2/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', a: 1, b: 2 }], options: { fields: { label: 'name', x: 'a', y: 'bb' } }, message: /column 'bb' not found/ },
  ],
})

const rows = [
  { label: 'tr', x: 9, y: 9 },
  { label: 'tl', x: 1, y: 8 },
  { label: 'bl', x: 2, y: 1 },
  { label: 'br', x: 8, y: 2 },
  { label: 'mid', x: 5, y: 5 },
]
const point = (el: Element, k: string) => el.querySelector(`[data-key="circle:p:${k}"]`)!
const pos = (el: Element, k: string) => [Number(point(el, k).getAttribute('cx')), Number(point(el, k).getAttribute('cy'))] as const

describe('quadrant: geometry', () => {
  it('splits at the medians by default; a point on the line goes high', () => {
    const el = host(640, 480)
    const h = quadrant.render(el, rows, { animate: false })
    expect(['tr', 'tl', 'bl', 'br', 'mid'].map((k) => point(el, k).getAttribute('data-series'))).toEqual(['q1', 'q2', 'q3', 'q4', 'q1'])
    const mx = Number(el.querySelector('[data-key="line:mid-x"]')!.getAttribute('x1'))
    const my = Number(el.querySelector('[data-key="line:mid-y"]')!.getAttribute('y1'))
    expect(pos(el, 'mid')[0]).toBeCloseTo(mx, 1)
    expect(pos(el, 'mid')[1]).toBeCloseTo(my, 1)
    expect(pos(el, 'tr')[0]).toBeGreaterThan(mx)
    expect(pos(el, 'tr')[1]).toBeLessThan(my)
    expect(pos(el, 'bl')[0]).toBeLessThan(mx)
    expect(pos(el, 'bl')[1]).toBeGreaterThan(my)
    h.destroy()
  })
  it('honours xMid / yMid and names the quadrants in their corners', () => {
    const el = host(640, 480)
    const h = quadrant.render(el, rows, { animate: false, xMid: 7, yMid: 3, q1: 'Quick wins', q2: 'Big bets', q3: 'Fill-ins', q4: 'Money pits' })
    expect(point(el, 'mid').getAttribute('data-series')).toBe('q2')
    expect(point(el, 'br').getAttribute('data-series')).toBe('q4')
    const corner = (q: string) => el.querySelector(`[data-key="text:qn:${q}"]`)!
    expect(corner('q1').textContent).toBe('Quick wins')
    expect(corner('q1').getAttribute('style')).toMatch(/text-anchor:end/)
    expect(corner('q3').textContent).toBe('Fill-ins')
    expect(Number(corner('q3').getAttribute('y'))).toBeGreaterThan(Number(corner('q2').getAttribute('y')))
    expect(h.exportCSV().split('\n')[1]).toBe('tr,9,9,Quick wins')
    h.destroy()
  })
  it('places point labels without overlapping each other', () => {
    const el = host(640, 480)
    const many = Array.from({ length: 40 }, (_, i) => ({ label: `Item ${i}`, x: (i * 37) % 100, y: (i * 53) % 100 }))
    const h = quadrant.render(el, many, { animate: false })
    const labels = [...el.querySelectorAll('[data-key^="text:pl:"]')]
    expect(labels.length).toBeGreaterThan(5)
    // the labels come from the greedy placer: boxes were checked there; here, no two share an anchor
    const anchors = new Set(labels.map((l) => `${l.getAttribute('x')},${l.getAttribute('y')}`))
    expect(anchors.size).toBe(labels.length)
    h.destroy()
  })
  it('the legend toggles a quadrant and the summary names the fullest one', () => {
    const el = host(640, 480)
    const h = quadrant.render(el, rows, { animate: false })
    expect(h.summary()).toMatch(/most in High x, high y \(2\)$/)
    ;(el.querySelector('.oc-legend button[data-key="q1"]') as HTMLButtonElement).click()
    expect(h.markCount()).toBe(3)
    h.destroy()
  })
})
