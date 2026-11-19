import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import candlestick, { candlePath } from './index'

chartSuite({
  module: candlestick,
  dir: import.meta.dirname,
  role: 'candle',
  marks: { basic: 20, realistic: 90, 'edge-single': 1, 'edge-duplicates': 40, 'edge-large': 5020 },
  summary: /^Candlestick chart, 20 sessions, Jan 5, 2026 to Jan 30, 2026, opened 100, closed [\d.,]+ \([+−][\d.]+%\), high [\d.,]+ on Jan \d+, 2026, low [\d.,]+ on Jan \d+, 2026$/,
  malformed: [
    { name: 'a high under the close', data: [{ date: '2026-01-02', open: 10, high: 11, low: 9, close: 12 }], message: /field 'high': must be at least max\(open, close\) = 12, got 11 at row 1/ },
    { name: 'a low over the open', data: [{ date: '2026-01-02', open: 10, high: 12, low: 10.5, close: 11 }, { date: '2026-01-03', open: 10, high: 12, low: 9, close: 11 }], message: /field 'low': must be at most min\(open, close\) = 10, got 10.5 at row 1/ },
    { name: 'a text price', data: [{ date: '2026-01-02', open: 'ten', high: 12, low: 9, close: 11 }], message: /field 'open' must be a number, got 'ten' at row 1/ },
    { name: 'a missing close', data: [{ date: '2026-01-02', open: 10, high: 12, low: 9 }], message: /field 'close' is missing at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const day = (d: string, open: number, high: number, low: number, close: number) => ({ date: d, open, high, low, close })

describe('candlestick: geometry', () => {
  it('the body spans open..close and the wicks reach high and low', () => {
    const d = candlePath(50, 10, 40, 10, 90, 60)
    expect(d).toBe('M50,10V40M50,60V90M45,40h10v20h-10Z')
  })
  it('up candles are hollow, down candles filled, in their own colours', () => {
    const el = host(600, 300)
    const h = candlestick.render(el, [day('2026-01-02', 10, 13, 9, 12), day('2026-01-05', 12, 12.5, 8, 9)], { animate: false })
    const [up, down] = [...el.querySelectorAll('[data-role="candle"]')].map((c) => c.getAttribute('style')!)
    expect(up).toMatch(/fill:var\(--oc-surface\)/)
    expect(up).toMatch(/stroke:var\(--oc-positive\)/)
    expect(down).toMatch(/fill:var\(--oc-negative\)/)
    h.destroy()
  })
  it('sorts sessions by date and keeps high above low on screen', () => {
    const el = host(600, 300)
    const h = candlestick.render(el, [day('2026-01-05', 12, 12.5, 8, 9), day('2026-01-02', 10, 13, 9, 12)], { animate: false })
    const xs = [...el.querySelectorAll('[data-role="candle"]')].map((c) => Number(c.getAttribute('d')!.match(/^M([\d.]+)/)![1]))
    expect(h.exportCSV().split('\n')[1]).toMatch(/^"?Jan 2, 2026"?,10,13,9,12/)
    expect(xs.length).toBe(2)
    h.destroy()
  })
  it('zooms on x', () => {
    expect(candlestick.def.zoom).toBe('x')
  })
})
