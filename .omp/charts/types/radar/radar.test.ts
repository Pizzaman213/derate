import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import radar from './index'

chartSuite({
  module: radar,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 12, realistic: 28, 'edge-single': 1, 'edge-duplicates': 12, 'edge-large': 192 },
  summary: /^Radar chart, 2 series over 6 axes \(Speed, Strength, Stamina, Agility and 2 more\), highest Alice on Agility at 9, lowest Bruno on Agility at 4$/,
  malformed: [
    { name: 'a text value', data: [{ axis: 'A', value: 1 }, { axis: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { axis: ['a', 'b'], value: [1] }, message: /column 'value' has 1 values but column 'axis' has 2/ },
    { name: 'a mapping to a missing column', data: [{ skill: 'A', score: 1 }], options: { fields: { axis: 'skill', value: 'scor' } }, message: /column 'scor' not found/ },
  ],
})

const centre = (el: Element) => {
  const spoke = el.querySelector('[data-key^="line:sp:"]')!
  return [Number(spoke.getAttribute('x1')), Number(spoke.getAttribute('y1'))] as const
}
const radii = (el: Element) => {
  const [cx, cy] = centre(el)
  return Object.fromEntries([...el.querySelectorAll('[data-role="point"]')].map((p) => [p.getAttribute('data-key')!.replace('circle:p:', ''), Math.hypot(Number(p.getAttribute('cx')) - cx, Number(p.getAttribute('cy')) - cy)]))
}

describe('radar: geometry', () => {
  const rows = [
    { axis: 'a', series: 's', value: 2 },
    { axis: 'b', series: 's', value: 5 },
    { axis: 'c', series: 's', value: 10 },
    { axis: 'd', series: 's', value: 0 },
  ]
  it('vertex radii are proportional to value / max', () => {
    const el = host(640, 480)
    const h = radar.render(el, rows, { animate: false, max: 20 })
    const r = radii(el)
    const outer = r['s:c']! * 2 // 10 of max 20 sits halfway out
    expect(r['s:a']! / outer).toBeCloseTo(2 / 20, 3)
    expect(r['s:b']! / outer).toBeCloseTo(5 / 20, 3)
    expect(r['s:d']!).toBeCloseTo(0, 3)
    // the outer ring is at max: its radius equals the spoke length
    const sp = el.querySelector('[data-key="line:sp:a"]')!
    const [cx, cy] = centre(el)
    expect(Math.hypot(Number(sp.getAttribute('x2')) - cx, Number(sp.getAttribute('y2')) - cy)).toBeCloseTo(outer, 3)
    h.destroy()
  })
  it('defaults max to the nice data maximum and clamps values above an explicit max', () => {
    const el = host(640, 480)
    const h = radar.render(el, rows, { animate: false })
    const r = radii(el)
    expect(r['s:b']! / r['s:c']!).toBeCloseTo(0.5, 3) // max = 10
    h.update(rows, { max: 5 })
    const r2 = radii(el)
    expect(r2['s:c']!).toBeCloseTo(r2['s:b']!, 3)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 value above the max drawn at the outer ring/)
    h.destroy()
  })
  it('spokes are evenly spaced, the first pointing up', () => {
    const el = host(640, 480)
    const h = radar.render(el, rows, { animate: false })
    const [cx, cy] = centre(el)
    const angles = [...el.querySelectorAll('[data-key^="line:sp:"]')].map((s) => Math.atan2(Number(s.getAttribute('y2')) - cy, Number(s.getAttribute('x2')) - cx))
    expect(angles[0]).toBeCloseTo(-Math.PI / 2, 6)
    expect(angles[1]).toBeCloseTo(0, 6)
    expect(Math.abs(angles[2]!)).toBeCloseTo(Math.PI / 2, 6)
    h.destroy()
  })
  it('accepts wide rows and toggles a series from the legend', () => {
    const el = host(640, 480)
    const h = radar.render(el, [{ name: 'X', speed: 1, power: 2, range: 3 }, { name: 'Y', speed: 3, power: 2, range: 1 }], { animate: false })
    expect(h.markCount()).toBe(2 + 6)
    expect(h.exportCSV()).toBe('Series,speed,power,range\nX,1,2,3\nY,3,2,1')
    ;(el.querySelector('.oc-legend button[data-key="Y"]') as HTMLButtonElement).click()
    expect(h.markCount()).toBe(1 + 3)
    h.destroy()
  })
  it('breaks the outline at a missing vertex and does not fill it', () => {
    const el = host(640, 480)
    const h = radar.render(el, [{ axis: 'a', value: 1 }, { axis: 'b', value: null }, { axis: 'c', value: 3 }, { axis: 'd', value: 2 }], { animate: false })
    const area = el.querySelector('[data-role="area"]')!
    expect(area.getAttribute('d')).not.toMatch(/Z/)
    expect(area.getAttribute('style')).toMatch(/fill:none/)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 missing value left out/)
    h.destroy()
  })
})
