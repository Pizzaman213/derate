import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { toTree } from '../_shared/PartWholeB'
import nestedProportions, { nest, nestedFromRows } from './index'

chartSuite({
  module: nestedProportions,
  dir: import.meta.dirname,
  role: 'square',
  marks: { basic: 4, realistic: 5, 'edge-single': 1, 'edge-negatives': 3, 'edge-nulls': 4, 'edge-duplicates': 3, 'edge-large': 9, 'edge-outliers': 3, 'edge-long-labels': 3, 'edge-unicode-rtl': 3 },
  summary: /^Nested proportions, Applicants 100 → Phone screen 55 \(55%\) → Interviewed 40 \(73%\) → Hired 12 \(30%\)$/,
  empty: ['edge-empty', 'edge-zeros'],
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 10 }, { label: 'B', value: 'some' }], message: /field 'value' must be a number, got 'some' at row 2/ },
    { name: 'a scalar', data: true, message: /the data must be a tree/ },
    { name: 'an unknown parent', data: [{ label: 'A', value: 10 }, { label: 'B', value: 2, parent: 'Q' }], message: /parent 'Q' not found/ },
  ],
})

const squares = (el: Element) => [...el.querySelectorAll('[data-role="square"][data-mark]')].map((r) => ({ key: r.getAttribute('data-key')!.slice(5), x: +r.getAttribute('x')!, y: +r.getAttribute('y')!, w: +r.getAttribute('width')!, h: +r.getAttribute('height')! }))

describe('nested-proportions: geometry', () => {
  it('square areas are proportional to values, squares share the bottom-left corner', () => {
    const el = host(640, 360)
    const h = nestedProportions.render(el, [{ label: 'Applicants', value: 100 }, { label: 'Interviewed', value: 40 }, { label: 'Hired', value: 12 }], { animate: false, height: 360 })
    const [a, b, c] = squares(el)
    for (const s of [a!, b!, c!]) expect(s.w).toBeCloseTo(s.h, 6)
    expect((b!.w * b!.h) / (a!.w * a!.h)).toBeCloseTo(0.4, 3)
    expect((c!.w * c!.h) / (b!.w * b!.h)).toBeCloseTo(0.3, 3)
    expect(b!.x).toBe(a!.x)
    expect(c!.x).toBe(a!.x)
    expect(b!.y + b!.h).toBeCloseTo(a!.y + a!.h, 6)
    expect(c!.y + c!.h).toBeCloseTo(a!.y + a!.h, 6)
    h.destroy()
  })
  it('label/value rows without a parent read as a chain; with parents, as a tree', () => {
    const chain = nest(toTree([{ label: 'A', value: 10 }, { label: 'B', value: 5 }, { label: 'C', value: 1 }], {}, nestedFromRows))
    expect(chain.panels.map((p) => p.map((n) => n.name))).toEqual([['A', 'B', 'C']])
    const tree = nest(toTree([{ label: 'A', value: 10 }, { label: 'B', value: 5, parent: 'A' }, { label: 'C', value: 1, parent: 'A' }], {}, nestedFromRows))
    expect(tree.panels.map((p) => p.map((n) => n.name))).toEqual([['A', 'B'], ['A', 'C']])
  })
  it('several parts of one whole: one panel each, the whole drawn once as data and repeated as context', () => {
    const el = host(800, 400)
    const h = nestedProportions.render(el, { name: 'All', value: 50, children: [{ name: 'x', value: 20 }, { name: 'y', value: 5 }] }, { animate: false, height: 400 })
    expect(h.markCount()).toBe(3)
    expect(el.querySelectorAll('[data-role="context"]')).toHaveLength(1)
    const s = squares(el)
    const whole = s.find((q) => q.key === 'All')!
    const y = s.find((q) => q.key === 'All › y')!
    expect((y.w * y.h) / (whole.w * whole.h)).toBeCloseTo(0.1, 3)
    h.destroy()
  })
  it('a whole without its own value is a label over independent wholes', () => {
    const m = nest({ name: 'Group', children: [{ name: 'P', value: 10, children: [{ name: 'p', value: 2 }] }, { name: 'Q', value: 4 }] })
    expect(m.wholes.map((w) => w.name)).toEqual(['P', 'Q'])
    expect(m.panels.map((p) => p.map((n) => n.name))).toEqual([['P', 'p'], ['Q']])
  })
  it('drops negatives and moves their parts up; groups a long tail as Other', () => {
    const neg = nest(toTree([{ label: 'A', value: 10 }, { label: 'B', value: -1 }, { label: 'C', value: 3 }], {}, nestedFromRows))
    expect(neg.panels.map((p) => p.map((n) => n.name))).toEqual([['A', 'C']])
    expect(neg.negatives).toBe(1)
    const many = nest({ name: 'W', value: 100, children: Array.from({ length: 12 }, (_, i) => ({ name: `k${i}`, value: 12 - i })) }, 5)
    expect(many.wholes[0]!.children.map((c) => c.name)).toEqual(['k0', 'k1', 'k2', 'k3', 'Other (8)'])
    expect(many.wholes[0]!.children[4]!.value).toBe(8 + 7 + 6 + 5 + 4 + 3 + 2 + 1)
    expect(many.grouped).toBe(8)
  })
  it('leaves an overlapping tail out instead of grouping it into an Other larger than the whole', () => {
    const m = nest({ name: 'Users', value: 100, children: Array.from({ length: 6 }, (_, i) => ({ name: `feature ${i}`, value: 60 - i })) }, 3)
    expect(m.wholes[0]!.children.map((c) => c.name)).toEqual(['feature 0', 'feature 1'])
    expect([m.grouped, m.hiddenParts]).toEqual([0, 4])
  })
  it('notes what it left out', () => {
    const el = host()
    const h = nestedProportions.render(el, [{ label: 'A', value: 10 }, { label: 'B', value: -2 }, { label: 'C', value: 0 }, { label: 'D', value: 1 }], { animate: false })
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 negative values omitted; 1 zero values not drawn')
    expect(h.markCount()).toBe(2)
    h.destroy()
  })
})
