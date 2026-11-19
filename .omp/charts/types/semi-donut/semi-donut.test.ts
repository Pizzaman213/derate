import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { arcAngles } from '../_shared/PartWholeA'
import semiDonut from './index'

chartSuite({
  module: semiDonut,
  dir: import.meta.dirname,
  role: 'arc',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 5, realistic: 6, 'edge-single': 1, 'edge-duplicates': 5, 'edge-negatives': 3, 'edge-nulls': 2, 'edge-large': 6 },
  summary: /^Semi-donut chart, 5 parts totalling 1,035, largest Bravo at 33%, smallest Delta at 8\.7%$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const announce = (el: HTMLElement) => el.querySelector('[aria-live]')!.textContent ?? ''

describe('semi-donut: geometry', () => {
  it('spans exactly 180°, from 9 to 3 o\'clock, in proportion to the shares', () => {
    const vals = [2, 1, 1]
    const a = arcAngles(vals, -Math.PI / 2, Math.PI)
    expect(a[0]!.start).toBeCloseTo(-Math.PI / 2, 12)
    expect(a[2]!.end).toBeCloseTo(Math.PI / 2, 12)
    a.forEach((d, i) => expect((d.end - d.start) / Math.PI).toBeCloseTo(vals[i]! / 4, 12))
  })
  it('keeps every slice above the flat edge and prints the total', () => {
    const el = host(320, 220)
    const h = semiDonut.render(el, [{ label: 'a', value: 30 }, { label: 'b', value: 70 }], { animate: false })
    expect(el.querySelector('[data-key="text:centre:v"]')!.textContent).toBe('100')
    expect(el.querySelector('[data-key="text:centre:c"]')!.textContent).toBe('Total')
    for (const p of el.querySelectorAll('[data-role="arc"]')) {
      // endpoints of every segment: M/L pairs, the last pair of each A
      for (const [, cmd, args] of (p.getAttribute('d') ?? '').matchAll(/([MLAZ])([^MLAZ]*)/g)) {
        const n = (args ?? '').split(/[ ,]+/).filter(Boolean).map(Number)
        const ys = cmd === 'A' ? [n[n.length - 1]!] : n.filter((_, i) => i % 2 === 1)
        for (const y of ys) expect(y).toBeLessThanOrEqual(1e-6)
      }
    }
    h.destroy()
  })
  it('folds the tail above maxSlices, and the legend toggle re-normalises', () => {
    const el = host()
    const rows = Array.from({ length: 8 }, (_, i) => ({ label: `p${i}`, value: 8 - i }))
    const h = semiDonut.render(el, rows, { animate: false, maxSlices: 4 })
    expect(h.markCount()).toBe(4)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/5 smallest parts grouped as Other/)
    ;(el.querySelector('.oc-legend button[data-key="__other"]') as HTMLElement).click()
    expect(h.markCount()).toBe(3)
    h.focusMark(0)
    expect(announce(el)).toBe(`1 of 3: p0: 8 (${Math.round((8 / 21) * 100)}%)`)
    h.destroy()
  })
})
