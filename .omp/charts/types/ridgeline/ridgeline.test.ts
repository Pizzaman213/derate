import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { trapezoid } from '../_shared/DistributionA'
import ridgeline, { ridges } from './index'

const sample = (name: string) => JSON.parse(readFileSync(join(import.meta.dirname, 'samples', `${name}.json`), 'utf8')) as { data: unknown; options?: Record<string, unknown> }

chartSuite({
  module: ridgeline,
  dir: import.meta.dirname,
  role: 'ridge',
  marks: { basic: 5, realistic: 12, 'edge-single': 1, 'edge-zeros': 5, 'edge-large': 30 },
  summary: /^Ridgeline plot, 5 groups, highest median Fri at [\d.]+, lowest median \w{3} at [\d.]+$/,
  malformed: [
    { name: 'a text value', data: [{ group: 'A', value: 1 }, { group: 'A', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ month: 'Jan', t: 1 }], options: { fields: { group: 'month', value: 'temp' } }, message: /column 'temp' not found/ },
  ],
})

/** [min y, max y] over a path's coordinate pairs. */
const ySpan = (el: Element, key: string): [number, number] => {
  const ys = el.querySelector(`[data-key="path:r:${key}"]`)!.getAttribute('d')!.match(/-?[\d.]+/g)!.map(Number).filter((_, i) => i % 2 === 1)
  return [Math.min(...ys), Math.max(...ys)]
}
const defaults = { bandwidth: 'silverman', sort: 'none', maxGroups: 30 } as const

describe('ridgeline: geometry', () => {
  it('rows are evenly spaced and the curves integrate to 1', () => {
    const el = host(640, 480)
    const h = ridgeline.render(el, sample('basic').data, { animate: false })
    const bases = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].map((k) => ySpan(el, k)[1])
    const steps = bases.slice(1).map((b, i) => b - bases[i]!)
    for (const s of steps) expect(s).toBeCloseTo(steps[0]!, 1)
    for (const r of ridges(ridgeline.validate(sample('basic').data).data!, defaults).list) expect(trapezoid(r.curve)).toBeCloseTo(1, 2)
    h.destroy()
  })
  it('shared scale: ridge heights are proportional to peak density; the tallest rises 1 + overlap rows', () => {
    const el = host(640, 480)
    const h = ridgeline.render(el, sample('basic').data, { animate: false, overlap: 1 })
    const { list } = ridges(ridgeline.validate(sample('basic').data).data!, defaults)
    const height = (k: string) => { const [a, b] = ySpan(el, k); return b - a }
    const step = ySpan(el, 'Tue')[1] - ySpan(el, 'Mon')[1]
    const tallest = list.reduce((a, r) => (r.peak.y > a.peak.y ? r : a))
    expect(height(tallest.group.name) / step).toBeCloseTo(2, 1)
    expect(height('Fri') / height('Mon')).toBeCloseTo(list[4]!.peak.y / list[0]!.peak.y, 2)
    h.destroy()
  })
  it("scale 'each': every ridge as tall as the tallest", () => {
    const el = host(640, 480)
    const h = ridgeline.render(el, sample('basic').data, { animate: false, scale: 'each', overlap: 0.5 })
    const hs = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].map((k) => { const [a, b] = ySpan(el, k); return b - a })
    for (const x of hs) expect(x).toBeCloseTo(hs[0]!, 0)
    h.destroy()
  })
  it('long group names truncate with the full name kept', () => {
    const el = host(400, 300)
    const h = ridgeline.render(el, sample('edge-long-labels').data, { animate: false })
    const labels = [...el.querySelectorAll('[data-key^="text:l:"]')]
    expect(labels.length).toBeGreaterThan(0)
    expect(labels.every((t) => t.textContent!.endsWith('…'))).toBe(true)
    h.destroy()
  })
  it('sort ascending puts the lowest median on top', () => {
    const { list } = ridges(ridgeline.validate(sample('basic').data).data!, { ...defaults, sort: 'ascending' })
    const meds = list.map((r) => r.stats.median)
    expect(meds).toEqual([...meds].sort((a, b) => a - b))
  })
})
