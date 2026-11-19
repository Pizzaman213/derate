import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import linearProgress, { progressShare, progressValue } from './index'

chartSuite({
  module: linearProgress,
  dir: import.meta.dirname,
  role: 'progress',
  marks: { basic: 5, realistic: 5, 'edge-single': 1, 'edge-zeros': 3, 'edge-nulls': 4, 'edge-duplicates': 2, 'edge-outliers': 3 },
  summary: /^Progress bar, 5 bars, highest Platform at 82%, lowest Support at 0%, 1 with no data$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'a', value: 'most' }], message: /field 'value' must be a number, got 'most' at row 1/ },
    { name: 'a negative max', data: { value: 1, max: -3 }, message: /max must be greater than 0, got -3/ },
    { name: 'a part without a value', data: { segments: [{ label: 'Done' }] }, message: /segments/ },
    { name: 'a mapping to a missing column', data: 'job,pct\na,1', options: { fields: { value: 'percent' } }, message: /column 'percent' not found/ },
  ],
})

const widthOf = (el: Element, key: string) => Number(el.querySelector(`[data-key="${key}"]`)?.getAttribute('width') ?? NaN)

describe('linear-progress: geometry', () => {
  it('fill width is value / max of the track, capped at full', () => {
    const el = host(640, 200)
    const h = linearProgress.render(el, [{ label: 'a', value: 25 }, { label: 'b', value: 50, max: 200 }, { label: 'c', value: 300 }], { animate: false })
    const track = widthOf(el, 'rect:a:track')
    expect(widthOf(el, 'rect:a:fill') / track).toBeCloseTo(0.25, 2)
    expect(widthOf(el, 'rect:b:fill') / track).toBeCloseTo(0.25, 2)
    expect(widthOf(el, 'rect:c:fill')).toBeCloseTo(track, 5)
    expect(el.querySelector('[data-key="text:c:readout"]')!.textContent).toBe('300%')
    h.destroy()
  })
  it('missing is a dashed empty track saying No data; zero is a solid track saying 0%', () => {
    const el = host(640, 200)
    const h = linearProgress.render(el, [{ label: 'gone', value: null }, { label: 'zero', value: 0 }], { animate: false })
    expect(el.querySelector('[data-key="rect:gone:track"]')!.getAttribute('style')).toMatch(/stroke-dasharray/)
    expect(el.querySelector('[data-key="rect:zero:track"]')!.getAttribute('style')).not.toMatch(/stroke-dasharray/)
    expect(el.querySelector('[data-key="text:gone:readout"]')!.textContent).toBe('No data')
    expect(el.querySelector('[data-key="text:zero:readout"]')!.textContent).toBe('0%')
    expect(el.querySelector('[data-key="rect:zero:fill"]')).toBeNull()
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 value missing \(dashed track\)/)
    h.destroy()
  })
  it('parts stack in order, sum to the value, and share colours across bars', () => {
    const d = { label: 's', max: 50, value: null, segments: [{ label: 'Done', value: 20 }, { label: 'Review', value: 10 }] }
    expect(progressValue(d)).toBe(30)
    expect(progressShare(d)).toBeCloseTo(0.6)
    const el = host(640, 200)
    const h = linearProgress.render(el, [d, { label: 't', max: 50, segments: [{ label: 'Review', value: 5 }] }], { animate: false })
    const parts = [...el.querySelectorAll('[data-role="segment"]')]
    expect(parts.length).toBe(3)
    const [done, review] = parts.map((p) => ({ x: Number(p.getAttribute('x')), w: Number(p.getAttribute('width')) }))
    expect(review!.x).toBeCloseTo(done!.x + done!.w, 5)
    expect(review!.w / done!.w).toBeCloseTo(0.5, 5)
    expect(el.querySelectorAll('.oc-legend li').length).toBe(2)
    h.destroy()
  })
  it('steps cut the track into equal parts', () => {
    const el = host(640, 120)
    const h = linearProgress.render(el, { value: 2, max: 4, segments: 4 }, { animate: false })
    expect(el.querySelectorAll('[data-key^="line:p1:step"]').length).toBe(3)
    h.destroy()
  })
})
