import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import boxPlot, { boxes } from './index'

const sample = (name: string) => JSON.parse(readFileSync(join(import.meta.dirname, 'samples', `${name}.json`), 'utf8')) as { data: unknown; options?: Record<string, unknown> }

chartSuite({
  module: boxPlot,
  dir: import.meta.dirname,
  role: 'box',
  marks: { basic: 4, realistic: 6, 'edge-single': 1, 'edge-zeros': 4, 'edge-large': 6006 },
  summary: /^Box plot, 4 groups, highest median Bravo at 37, lowest median Delta at 14\.5, 2 outliers$/,
  malformed: [
    { name: 'a text value', data: [{ group: 'A', value: 1 }, { group: 'A', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { group: ['a', 'b'], value: [1] }, message: /column 'value' has 1 values but column 'group' has 2/ },
    { name: 'a mapping to a missing column', data: [{ dept: 'A', pay: 1 }], options: { fields: { group: 'dept', value: 'salary' } }, message: /column 'salary' not found/ },
  ],
})

const basic = () => boxPlot.validate(sample('basic').data).data!
const defaults = { sort: 'none', maxGroups: 30, whisker: 1.5 } as const

describe('box-plot: statistics', () => {
  it('quartiles follow R type 7 and whiskers stop at 1.5 IQR', () => {
    const { list } = boxes(basic(), defaults)
    const alpha = list[0]!.box, charlie = list[2]!.box
    expect([alpha.q1, alpha.median, alpha.q3]).toEqual([17.5, 21, 24.5])
    expect(alpha.outliers).toEqual([48])
    expect(alpha.upperWhisker).toBe(27)
    expect([charlie.q1, charlie.median, charlie.q3]).toEqual([20.25, 22.5, 25.75])
    expect(charlie.outliers).toEqual([5])
    expect(charlie.lowerWhisker).toBe(18)
  })
  it('a wider whisker factor absorbs the outliers', () => {
    const { list } = boxes(basic(), { ...defaults, whisker: 4 })
    expect(list.flatMap((g) => g.box.outliers)).toEqual([])
  })
  it('sorts by median and pools the tail into Other', () => {
    expect(boxes(basic(), { ...defaults, sort: 'descending' }).list.map((g) => g.group.name)).toEqual(['Bravo', 'Charlie', 'Alpha', 'Delta'])
    const many = Array.from({ length: 40 }, (_, i) => ({ group: `g${i}`, value: i }))
    const r = boxes(boxPlot.validate(many).data!, { ...defaults, maxGroups: 10 })
    expect(r.list.length).toBe(10)
    expect(r.list[9]!.group.name).toBe('Other')
    expect(r.collapsed).toBe(31)
  })
})

describe('box-plot: geometry', () => {
  const at = (el: Element, key: string) => el.querySelector(`[data-key="${key}"]`)!
  it('the box spans Q1..Q3 on the value axis and the median line sits at the median', () => {
    const el = host(640, 400)
    const h = boxPlot.render(el, sample('basic').data, { animate: false, yDomain: [0, 50] })
    const y0 = Number(at(el, 'line:gy:0').getAttribute('y1')), y50 = Number(at(el, 'line:gy:50').getAttribute('y1'))
    const val = (py: number) => ((py - y0) / (y50 - y0)) * 50
    const box = at(el, 'rect:box:Alpha')
    const top = Number(box.getAttribute('y')), bottom = top + Number(box.getAttribute('height'))
    expect(val(top)).toBeCloseTo(24.5, 1)
    expect(val(bottom)).toBeCloseTo(17.5, 1)
    expect(val(Number(at(el, 'line:med:Alpha').getAttribute('y1')))).toBeCloseTo(21, 1)
    expect(val(Number(at(el, 'circle:o:Alpha:0').getAttribute('cy')))).toBeCloseTo(48, 1)
    expect(el.querySelectorAll('[data-role="outlier"]').length).toBe(2)
    h.destroy()
  })
  it('horizontal orientation puts values on x', () => {
    const el = host(640, 400)
    const h = boxPlot.render(el, sample('basic').data, { animate: false, orientation: 'horizontal', xDomain: [0, 50] })
    const x0 = Number(at(el, 'line:gx:0').getAttribute('x1')), x50 = Number(at(el, 'line:gx:50').getAttribute('x1'))
    const box = at(el, 'rect:box:Bravo')
    const left = Number(box.getAttribute('x'))
    expect(((left - x0) / (x50 - x0)) * 50).toBeCloseTo(33.5, 1)
    h.destroy()
  })
  it('points: all draws every value once, outliers keep their role', () => {
    const el = host()
    const h = boxPlot.render(el, sample('basic').data, { animate: false, points: 'all', showMean: true })
    expect(el.querySelectorAll('[data-role="point"]').length + el.querySelectorAll('[data-role="outlier"]').length).toBe(41)
    expect(el.querySelectorAll('[data-key^="path:mean:"]').length).toBe(4)
    h.destroy()
  })
})
