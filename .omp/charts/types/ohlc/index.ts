// OHLC bars: one vertical bar per session from low to high, a tick left at the open and
// a tick right at the close. Direction is readable from the ticks alone; colour repeats it.

import { defineChart } from '../../core'
import { describeOhlc, drawOhlc, tableOhlc } from '../_shared/TrendB'
import { input, options, type OhlcOptions, type OhlcRow } from './schema'

const r2 = (v: number) => Math.round(v * 100) / 100

/** Path of one OHLC bar: the high-low stem, the open tick (left) and the close tick (right). */
export function ohlcPath(x: number, w: number, open: number, high: number, low: number, close: number): string {
  const t = w / 2
  return `M${r2(x)},${r2(high)}V${r2(Math.max(low, high + 1))}M${r2(x - t)},${r2(open)}H${r2(x)}M${r2(x)},${r2(close)}H${r2(x + t)}`
}

export const ohlc = defineChart<OhlcRow[], OhlcOptions>({
  id: 'ohlc',
  name: 'OHLC chart',
  category: 'trend',
  intents: ['trend', 'change-over-time'],
  summary: 'Open, high, low and close per session as tick bars, with optional volume.',
  input,
  options,
  shape: 'rows',
  fields: ['date', 'open', 'high', 'low', 'close', 'volume'],
  dateFields: ['date'],
  numberFields: ['open', 'high', 'low', 'close', 'volume'],
  zoom: 'x',
  aspect: 0.55,
  minHeight: 200,
  draw(ctx) {
    return drawOhlc(ctx, (g) => ({
      type: 'path', role: 'ohlc', key: g.key, d: ohlcPath(g.x, g.w, g.open, g.high, g.low, g.close),
      fill: 'none', stroke: g.color, strokeWidth: g.w < 4 ? 1 : 1.5, linecap: 'butt',
      cx: g.x, cy: (g.high + g.low) / 2, box: { x: g.x - g.w / 2 - 1, y: g.high, width: g.w + 2, height: Math.max(2, g.low - g.high) },
      datum: g.row, label: g.label, tooltip: g.tooltip,
    }))
  },
  describe: ({ data, options: o, fmt, name }) => describeOhlc(data, o, fmt, name),
  table: (data, o, fmt) => tableOhlc(data, o, fmt),
})

export default ohlc
