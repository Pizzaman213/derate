import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import ohlc, { ohlcPath } from './index'

chartSuite({
  module: ohlc,
  dir: import.meta.dirname,
  role: 'ohlc',
  marks: { basic: 20, realistic: 90, 'edge-single': 1, 'edge-duplicates': 40, 'edge-large': 5020 },
  summary: /^OHLC chart, 20 sessions, Jan 5, 2026 to Jan 30, 2026, opened 100, closed [\d.,]+ \([+−][\d.]+%\), high [\d.,]+ on Jan \d+, 2026, low [\d.,]+ on Jan \d+, 2026$/,
  malformed: [
    { name: 'a high under the close', data: [{ date: '2026-01-02', open: 10, high: 11, low: 9, close: 12 }], message: /field 'high': must be at least max\(open, close\) = 12, got 11 at row 1/ },
    { name: 'a low over the open', data: [{ date: '2026-01-02', open: 10, high: 12, low: 10.5, close: 11 }, { date: '2026-01-03', open: 10, high: 12, low: 9, close: 11 }], message: /field 'low': must be at most min\(open, close\) = 10, got 10.5 at row 1/ },
    { name: 'a text price', data: [{ date: '2026-01-02', open: 'ten', high: 12, low: 9, close: 11 }], message: /field 'open' must be a number, got 'ten' at row 1/ },
    { name: 'a missing close', data: [{ date: '2026-01-02', open: 10, high: 12, low: 9 }], message: /field 'close' is missing at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const day = (d: string, open: number, high: number, low: number, close: number) => ({ date: d, open, high, low, close })

describe('ohlc: geometry', () => {
  it('stem from high to low, open tick left, close tick right', () => {
    expect(ohlcPath(50, 10, 40, 10, 90, 60)).toBe('M50,10V90M45,40H50M50,60H55')
  })
  it('colours by direction and sorts by date', () => {
    const el = host(600, 300)
    const h = ohlc.render(el, [day('2026-01-05', 12, 12.5, 8, 9), day('2026-01-02', 10, 13, 9, 12)], { animate: false })
    const styles = [...el.querySelectorAll('[data-role="ohlc"]')].map((c) => c.getAttribute('style')!)
    expect(styles[0]).toMatch(/stroke:var\(--oc-positive\)/)
    expect(styles[1]).toMatch(/stroke:var\(--oc-negative\)/)
    h.destroy()
  })
  it('draws volume bars only where volume exists', () => {
    const el = host(600, 300)
    const h = ohlc.render(el, [{ ...day('2026-01-02', 10, 13, 9, 12), volume: 100 }, { ...day('2026-01-05', 12, 12.5, 8, 9), volume: null }], { animate: false })
    expect(el.querySelectorAll('[data-role="volume"]').length).toBe(1)
    h.destroy()
  })
})
