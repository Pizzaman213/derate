import { describe, expect, it } from 'vitest'
import { chart } from '../../index'
import { host } from '../host'

const rows = [1, 2, 4, 8].flatMap((x) => [
  { x, series: 'ceiling', y: 100 / x },
  { x, series: 'measured', y: 80 / x },
])

describe('multi-line options', () => {
  it('draws vertical markers and dashed series', () => {
    const el = host()
    const h = chart('multi-line', el, rows, { xType: 'number', animate: false, dashed: ['ceiling'], verticalMarkers: [{ x: 4, series: 'ceiling', label: 'KV full' }] })
    const svg = h.exportSVG()
    expect(svg).toContain('KV full')
    expect((svg.match(/stroke-dasharray/g) ?? []).length).toBeGreaterThanOrEqual(2)
    h.destroy?.()
  })
  it('draws pointsOnly series as dots with no line', () => {
    const el = host()
    const h = chart('multi-line', el, rows, { xType: 'number', animate: false, pointsOnly: ['measured'] })
    expect(el.querySelectorAll('path').length).toBeGreaterThan(0)
    const svg = h.exportSVG()
    expect(svg).not.toMatch(/stroke-dasharray/)
    // one line path for 'ceiling' only
    const lines = Array.from(el.querySelectorAll('path')).filter((p) => (p.getAttribute('d') ?? '').startsWith('M') && p.getAttribute('fill') === 'none')
    expect(lines.length).toBeLessThanOrEqual(1)
    h.destroy?.()
  })
  it('defaults keep earlier output', () => {
    const h = chart('multi-line', host(), rows, { xType: 'number', animate: false })
    expect(h.exportSVG()).not.toMatch(/stroke-dasharray/)
  })
})
