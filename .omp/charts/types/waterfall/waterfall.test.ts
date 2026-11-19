import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import waterfall, { waterfallSteps } from './index'
import { options } from './schema'
import realistic from './samples/realistic.json'

chartSuite({
  module: waterfall,
  dir: import.meta.dirname,
  role: 'bar',
  marks: { basic: 6, realistic: 12, 'edge-single': 1, 'edge-duplicates': 11, 'edge-large': 5201 },
  summary: /^Waterfall chart, 4 changes, from 500 to 615 \(net \+115\), largest increase New customers \+180, largest decrease Churn -120$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'an unknown kind', data: [{ label: 'A', value: 1, kind: 'subtotal' }], message: /kind/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const defaults = options.parse({})
const num = (el: Element, a: string) => Number(el.getAttribute(a))

describe('waterfall: running totals', () => {
  it('floats each change from the previous running total and appends the total', () => {
    const deltas = [100, -30, 45, -80, 12]
    const steps = waterfallSteps(deltas.map((value, i) => ({ label: `s${i}`, value })), defaults)
    let run = 0
    deltas.forEach((v, i) => {
      expect(steps[i]!.from).toBe(run)
      run += v
      expect(steps[i]!.to).toBe(run)
    })
    const total = steps[steps.length - 1]!
    expect(total).toMatchObject({ kind: 'total', label: 'Total', from: 0, appended: true })
    expect(total.to).toBe(deltas.reduce((a, b) => a + b, 0))
  })
  it('a total row equals the running total at that point; an opening total sets the start', () => {
    const steps = waterfallSteps([
      { label: 'Open', value: 200, kind: 'total' },
      { label: 'a', value: 50 },
      { label: 'Sub', value: null, kind: 'total' },
      { label: 'b', value: -75 },
      { label: 'Close', value: 999, kind: 'total' },
    ], defaults)
    expect(steps.map((s) => [s.label, s.from, s.to])).toEqual([['Open', 0, 200], ['a', 200, 250], ['Sub', 0, 250], ['b', 250, 175], ['Close', 0, 175]])
    // the given 999 disagreed: drawn at the running total and remembered for the note
    expect(steps[4]!.given).toBe(999)
    expect(steps.some((s) => s.appended)).toBe(false)
  })
  it('the realistic bridge ends at revenue minus every cost', () => {
    const rows = (realistic.data as string).trim().split('\n').slice(1).map((l) => l.split(','))
    const sum = rows.reduce((a, r) => a + (r[2] === 'delta' ? Number(r[1]) : 0), 0)
    const el = host(800, 400)
    const h = waterfall.render(el, realistic.data, { ...realistic.options, animate: false })
    const csv = h.exportCSV().trim().split('\n')
    expect(csv[csv.length - 1]).toBe(`Net income,total,,${sum}`)
    expect(csv.find((l) => l.startsWith('Gross profit'))).toBe(`Gross profit,total,,${48210 - 19874}`)
    h.destroy()
  })
  it('missing changes count as no change and are noted', () => {
    const steps = waterfallSteps([{ label: 'a', value: 10 }, { label: 'b', value: null }, { label: 'c', value: 5 }], defaults)
    expect(steps.map((s) => s.to)).toEqual([10, 10, 15, 15])
    const el = host()
    const h = waterfall.render(el, [{ label: 'a', value: 10 }, { label: 'b', value: null }, { label: 'c', value: 5 }], { animate: false })
    expect(h.markCount()).toBe(3)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 missing changes treated as no change/)
    h.destroy()
  })
})

describe('waterfall: geometry', () => {
  it('bars float: each starts where the previous ended; totals stand on zero', () => {
    const el = host(640, 400)
    const h = waterfall.render(el, [{ label: 'a', value: 100 }, { label: 'b', value: -40 }, { label: 'c', value: 20 }], { animate: false })
    const [a, b, c, t] = [...el.querySelectorAll('[data-role="bar"]')]
    const top = (r: Element) => num(r, 'y'), bottom = (r: Element) => num(r, 'y') + num(r, 'height')
    expect(top(b!)).toBeCloseTo(top(a!), 1) // the decrease hangs from a's top
    expect(top(c!)).toBeCloseTo(bottom(b!) - num(c!, 'height'), 1)
    expect(bottom(c!)).toBeCloseTo(bottom(b!), 1) // c rises from b's bottom
    expect(bottom(t!)).toBeCloseTo(bottom(a!), 1) // the total stands on zero
    expect(num(t!, 'height') / num(a!, 'height')).toBeCloseTo(0.8, 2)
    const fill = (r: Element) => /fill:([^;]+)/.exec(r.getAttribute('style') ?? '')?.[1]
    expect([fill(a!), fill(b!), fill(t!)]).toEqual(['var(--oc-positive)', 'var(--oc-negative)', 'var(--oc-neutral)'])
    // connectors carry the running total across
    expect(el.querySelectorAll('[data-key^="line:k:"]').length).toBe(3)
    h.destroy()
  })
  it('showTotal: false appends nothing', () => {
    const el = host()
    const h = waterfall.render(el, [{ label: 'a', value: 1 }, { label: 'b', value: 2 }], { animate: false, showTotal: false })
    expect(h.markCount()).toBe(2)
    h.destroy()
  })
})
