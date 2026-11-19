import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { arcAngles, prepare } from '../_shared/PartWholeA'
import pie from './index'

chartSuite({
  module: pie,
  dir: import.meta.dirname,
  role: 'arc',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 5, realistic: 6, 'edge-single': 1, 'edge-duplicates': 5, 'edge-negatives': 3, 'edge-nulls': 2, 'edge-large': 6 },
  summary: /^Pie chart, 5 slices totalling 1,035, largest Bravo at 33%, smallest Delta at 8\.7%$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', amount: 1 }], options: { fields: { label: 'name', value: 'amont' } }, message: /column 'amont' not found/ },
  ],
})

const announce = (el: HTMLElement) => el.querySelector('[aria-live]')!.textContent ?? ''

describe('pie: angles', () => {
  it('sum to 2π and each angle / 2π equals the share', () => {
    const vals = [120, 340, 210, 90, 275]
    const total = vals.reduce((s, v) => s + v, 0)
    const a = arcAngles(vals)
    expect(a[0]!.start).toBe(0)
    expect(a[a.length - 1]!.end).toBeCloseTo(Math.PI * 2, 12)
    expect(a.reduce((s, d) => s + (d.end - d.start), 0)).toBeCloseTo(Math.PI * 2, 12)
    a.forEach((d, i) => {
      expect((d.end - d.start) / (Math.PI * 2)).toBeCloseTo(vals[i]! / total, 12)
      expect(d.share).toBeCloseTo(vals[i]! / total, 12)
      if (i) expect(d.start).toBe(a[i - 1]!.end)
    })
  })
  it('the drawn slices carry those shares, largest first', () => {
    const el = host()
    const h = pie.render(el, [{ label: 'a', value: 1 }, { label: 'b', value: 3 }], { animate: false })
    h.focusMark(0)
    expect(announce(el)).toBe('1 of 2: b: 3 (75%)')
    h.focusMark(1)
    expect(announce(el)).toBe('2 of 2: a: 1 (25%)')
    h.destroy()
  })
})

describe('pie: cleaning', () => {
  it('groups the smallest into Other above maxSlices and notes it', () => {
    const rows = Array.from({ length: 9 }, (_, i) => ({ label: `s${i}`, value: 100 - i * 10 }))
    const p = prepare(rows, { maxSlices: 6, sort: 'descending' })
    expect(p.slices.length).toBe(6)
    expect(p.folded).toBe(4)
    expect(p.slices[5]!.label).toBe('Other (4)')
    expect(p.slices[5]!.value).toBe(50 + 40 + 30 + 20)
    expect(p.slices.reduce((s, d) => s + d.value, 0)).toBe(rows.reduce((s, d) => s + d.value, 0))
    const el = host()
    const h = pie.render(el, rows, { animate: false })
    expect(h.markCount()).toBe(6)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/4 smallest parts grouped as Other/)
    h.destroy()
  })
  it('omits negatives with a note and shows the empty state for all zeros', () => {
    const el = host()
    const h = pie.render(el, [{ label: 'a', value: 5 }, { label: 'b', value: -2 }, { label: 'c', value: 5 }], { animate: false })
    expect(h.markCount()).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 negative value omitted/)
    h.update([{ label: 'a', value: 0 }, { label: 'b', value: 0 }])
    expect(h.state).toBe('empty')
    expect(el.querySelector('.oc-state')!.textContent).toMatch(/All values are zero/)
    h.destroy()
  })
  it('legend toggle removes a slice and re-normalises the rest', () => {
    const el = host()
    const h = pie.render(el, [{ label: 'a', value: 1 }, { label: 'b', value: 1 }, { label: 'c', value: 2 }], { animate: false })
    expect(h.markCount()).toBe(3)
    ;(el.querySelector('.oc-legend button[data-key="c"]') as HTMLElement).click()
    expect(h.markCount()).toBe(2)
    h.focusMark(0)
    expect(announce(el)).toBe('1 of 2: a: 1 (50%)')
    ;(el.querySelector('.oc-legend button[data-key="c"]') as HTMLElement).click()
    expect(h.markCount()).toBe(3)
    h.destroy()
  })
})
