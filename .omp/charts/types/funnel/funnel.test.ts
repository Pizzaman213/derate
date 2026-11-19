import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import funnel, { conversions } from './index'

chartSuite({
  module: funnel,
  dir: import.meta.dirname,
  role: 'stage',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 5, realistic: 6, 'edge-single': 1, 'edge-negatives': 3, 'edge-nulls': 2, 'edge-duplicates': 5, 'edge-large': 60 },
  summary: /^Funnel chart, 5 stages from Visits at 12k to Purchase at 640, 5\.3% overall conversion, biggest drop Product views to Added to cart keeping 37\.8%$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const announce = (el: HTMLElement) => el.querySelector('[aria-live]')!.textContent ?? ''

describe('funnel: conversion', () => {
  it('computes from-first and from-previous ratios', () => {
    const c = conversions([1000, 400, 100, 0, 5])
    expect(c.map((d) => d.fromFirst)).toEqual([1, 0.4, 0.1, 0, 0.005])
    expect(c.map((d) => d.fromPrev)).toEqual([null, 0.4, 0.25, 0, null])
    expect(conversions([0, 5]).map((d) => d.fromFirst)).toEqual([null, null])
  })
  it('bar widths are proportional to values, centred on one axis', () => {
    const el = host(640, 400)
    const h = funnel.render(el, [{ label: 'a', value: 200 }, { label: 'b', value: 100 }, { label: 'c', value: 50 }], { animate: false })
    const r = [...el.querySelectorAll('[data-role="stage"]')].map((e) => ({ x: Number(e.getAttribute('x')), w: Number(e.getAttribute('width')) }))
    expect(r[1]!.w / r[0]!.w).toBeCloseTo(0.5, 3)
    expect(r[2]!.w / r[0]!.w).toBeCloseTo(0.25, 3)
    for (const b of r) expect(b.x + b.w / 2).toBeCloseTo(r[0]!.x + r[0]!.w / 2, 2)
    h.focusMark(2)
    expect(announce(el)).toBe('3 of 3: c: 50, 25% of first, 50% of previous')
    h.destroy()
  })
  it('legend toggle removes a stage and recomputes conversion', () => {
    const el = host()
    const h = funnel.render(el, [{ label: 'a', value: 200 }, { label: 'b', value: 100 }, { label: 'c', value: 50 }], { animate: false })
    ;(el.querySelector('.oc-legend button[data-key="b"]') as HTMLElement).click()
    expect(h.markCount()).toBe(2)
    h.focusMark(1)
    expect(announce(el)).toBe('2 of 2: c: 50, 25% of first, 25% of previous')
    h.destroy()
  })
  it('omits negatives with a note and keeps stage order', () => {
    const el = host()
    const h = funnel.render(el, [{ label: 'z', value: 9 }, { label: 'y', value: -1 }, { label: 'x', value: 3 }], { animate: false })
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 negative value omitted/)
    expect(h.exportCSV().split('\n')[1]).toBe('z,9,1,')
    h.destroy()
  })
})
