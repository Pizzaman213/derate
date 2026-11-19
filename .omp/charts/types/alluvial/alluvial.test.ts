import { describe, expect, it } from 'vitest'
import { chartSuite, host, loadSamples } from '../../tests/harness'
import alluvial from './index'

chartSuite({
  module: alluvial,
  dir: import.meta.dirname,
  role: 'flow',
  marks: { basic: 24, 'edge-single': 2, 'edge-duplicates': 24 },
  empty: ['edge-empty', 'edge-zeros'],
  summary: /^Alluvial diagram, 3 stages \(Class, Sex and Survived\), 2,201 in total, largest Male in Sex at 1,731 \(79%\)$/,
  malformed: [
    { name: 'a text value', data: [{ a: 'x', b: 'y', value: 1 }, { a: 'x', b: 'z', value: 'many' }], message: /field 'value' must be a number, got 'many' at row 2/ },
    { name: 'a single stage', data: [{ a: 'x', value: 1 }], message: /needs at least two stage columns, got only 'a'/ },
    { name: 'a missing stage column', data: [{ a: 'x', b: 'y', value: 1 }], options: { columns: ['a', 'c'] }, message: /stage column 'c' not found \(columns: a, b, value\)/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const num = (el: Element, a: string) => Number(el.getAttribute(a))
/** Heights of the band ends, from the path data: M x,y0 C ... x1,ty0 L x1,ty1 C ... x0,sy1 Z */
function band(d: string) {
  const n = d.match(/-?[\d.]+/g)!.map(Number)
  return { sy0: n[1]!, ty0: n[7]!, ty1: n[9]!, sy1: n[15]! }
}

describe('alluvial: geometry', () => {
  const basic = loadSamples(import.meta.dirname).find((s) => s.name === 'basic')!

  it('conserves the total at every stage, and each bar is as tall as what flows through it', () => {
    const el = host(900, 500)
    const h = alluvial.render(el, basic.data, { animate: false })
    const nodes = [...el.querySelectorAll('[data-role="node"]')]
    const stageHeight = (s: number) => nodes.filter((n) => n.getAttribute('data-key')!.startsWith(`rect:node:${s}:`)).reduce((acc, n) => acc + num(n, 'height'), 0)
    expect(stageHeight(1)).toBeCloseTo(stageHeight(0), 1)
    expect(stageHeight(2)).toBeCloseTo(stageHeight(0), 1)
    const flows = [...el.querySelectorAll('[data-role="flow"]')].map((f) => ({ key: f.getAttribute('data-key')!, ...band(f.getAttribute('d')!) }))
    for (const s of [0, 1]) {
      const out = flows.filter((f) => f.key.startsWith(`path:flow:${s}:`))
      expect(out.reduce((a, f) => a + (f.sy1 - f.sy0), 0)).toBeCloseTo(stageHeight(s), 1)
      expect(out.reduce((a, f) => a + (f.ty1 - f.ty0), 0)).toBeCloseTo(stageHeight(s + 1), 1)
    }
    // Male: everything in from Class equals everything out to Survived
    const male = num(el.querySelector('[data-key="rect:node:1:Male"]')!, 'height')
    const into = flows.filter((f) => /^path:flow:0:[^:]+:[^:]+:Male$/.test(f.key)).reduce((a, f) => a + (f.ty1 - f.ty0), 0)
    const outOf = flows.filter((f) => /^path:flow:1:[^:]+:Male:/.test(f.key)).reduce((a, f) => a + (f.sy1 - f.sy0), 0)
    expect(into).toBeCloseTo(male, 1)
    expect(outOf).toBeCloseTo(male, 1)
    h.destroy()
  })

  it('keeps the column order given, not the data order', () => {
    const el = host(900, 500)
    const h = alluvial.render(el, basic.data, { animate: false, columns: ['Survived', 'Class'] })
    const x = (key: string) => num(el.querySelector(`[data-key="${key}"]`)!, 'x')
    expect(x('rect:node:0:Yes')).toBeLessThan(x('rect:node:1:Crew'))
    expect(el.querySelector('[data-key="text:stage:0"]')!.textContent).toBe('Survived')
    h.destroy()
  })

  it('counts rows without a value column and groups the tail as Other', () => {
    const el = host(900, 500)
    const rows = Array.from({ length: 30 }, (_, i) => ({ from: `c${i}`, to: i % 2 ? 'odd' : 'even' }))
    const h = alluvial.render(el, rows, { animate: false, maxCategories: 5 })
    expect(el.querySelectorAll('[data-key^="rect:node:0:"]').length).toBe(5)
    expect(el.querySelector('[data-key="rect:node:0:Other"]')).not.toBeNull()
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/26 small categories grouped as "Other"/)
    expect(h.summary()).toMatch(/30 in total/)
    h.destroy()
  })
})
