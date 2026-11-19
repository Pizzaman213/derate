import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import parallelCoordinates from './index'

chartSuite({
  module: parallelCoordinates,
  dir: import.meta.dirname,
  role: 'line',
  marks: { basic: 10, realistic: 24, 'edge-single': 1, 'edge-duplicates': 10, 'edge-large': 5200 },
  summary: /^Parallel coordinates, 10 rows across 5 axes \(mpg, cylinders, horsepower, weight and zeroToSixty\), 3 series$/,
  malformed: [
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'an unknown column', data: [{ a: 1, b: 2 }], options: { columns: ['a', 'z'] }, message: /column 'z' not found \(columns: a, b\)/ },
    { name: 'a text column picked', data: [{ a: 1, b: 'x' }], options: { columns: ['b'] }, message: /column 'b' is not numeric/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', a: 1 }], options: { fields: { label: 'nam' } }, message: /column 'nam' not found/ },
  ],
})

const rows = [
  { label: 'lo', a: 0, b: 100, c: 5 },
  { label: 'mid', a: 5, b: 150, c: 5 },
  { label: 'hi', a: 10, b: 200, c: 5 },
]
const vertices = (d: string) => [...d.matchAll(/[ML](-?[\d.]+),(-?[\d.]+)/g)].map((m) => [Number(m[1]), Number(m[2])] as const)

describe('parallel-coordinates: geometry', () => {
  it('one polyline per row with a vertex on every axis; each axis has its own scale', () => {
    const el = host(640, 400)
    const h = parallelCoordinates.render(el, rows, { animate: false })
    const lines = [...el.querySelectorAll('[data-role="line"]')].map((p) => vertices(p.getAttribute('d')!))
    expect(lines.length).toBe(3)
    for (const l of lines) expect(l.length).toBe(3)
    // same x for every row on an axis, axes evenly spaced
    const x = lines[0]!.map((v) => v[0])
    for (const l of lines) expect(l.map((v) => v[0])).toEqual(x)
    expect(x[2]! - x[1]!).toBeCloseTo(x[1]! - x[0]!, 1)
    // a and b are on different scales but both linear in their own domain: mid sits halfway
    for (const axis of [0, 1]) expect(lines[1]![axis]![1]).toBeCloseTo((lines[0]![axis]![1] + lines[2]![axis]![1]) / 2, 1)
    // higher values are higher on screen
    expect(lines[2]![0]![1]).toBeLessThan(lines[0]![0]![1])
    // each axis carries its own tick labels
    expect(el.querySelector('[data-key="text:tl:b:200"]')).not.toBeNull()
    expect(el.querySelector('[data-key="text:tl:a:10"]')).not.toBeNull()
    h.destroy()
  })
  it('breaks the line at a missing value and notes it', () => {
    const el = host(640, 400)
    const h = parallelCoordinates.render(el, [{ a: 1, b: null, c: 3, d: 4 }, { a: 2, b: 2, c: 1, d: 1 }], { animate: false })
    const d = el.querySelector('[data-role="line"]')!.getAttribute('d')!
    expect(d.match(/M/g)!.length).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 row has missing values/)
    h.destroy()
  })
  it('hovering a line highlights it alone; the legend toggles a series', () => {
    const el = host(640, 400)
    const data = [{ series: 'x', a: 1, b: 2 }, { series: 'x', a: 2, b: 1 }, { series: 'y', a: 3, b: 3 }]
    const h = parallelCoordinates.render(el, data, { animate: false })
    const lines = [...el.querySelectorAll('[data-role="line"]')]
    expect(new Set(lines.map((l) => l.getAttribute('data-series'))).size).toBe(3)
    lines[0]!.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX: 1, clientY: 1 }))
    expect(lines.filter((l) => l.classList.contains('oc-dim')).length).toBe(2)
    ;(el.querySelector('.oc-legend button[data-key="x"]') as HTMLButtonElement).click()
    expect(h.markCount()).toBe(1)
    h.destroy()
  })
  it('switches to canvas above 5k lines', () => {
    const el = host()
    const data = Array.from({ length: 5100 }, (_, i) => ({ a: i, b: i % 23, c: i % 5 }))
    const h = parallelCoordinates.render(el, data, { animate: false })
    expect(h.renderer).toBe('canvas')
    expect(h.markCount()).toBe(5100)
    h.focusMark(5099)
    expect(el.querySelector('[aria-live]')!.textContent).toBe('5100 of 5100: Row 5100: a 5,099, b 16, c 4')
    h.destroy()
  })
})
