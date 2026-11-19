// Candlestick: one candle per session -- a body from open to close and wicks to the high
// and low. Up sessions are hollow in the positive colour, down sessions filled in the
// negative one, so direction survives without colour.

import { defineChart } from '../../core'
import { describeOhlc, drawOhlc, tableOhlc } from '../_shared/TrendB'
import { input, options, type CandleOptions, type CandleRow } from './schema'

const r2 = (v: number) => Math.round(v * 100) / 100

/** Path of one candle: upper wick, body rectangle, lower wick. */
export function candlePath(x: number, w: number, open: number, high: number, low: number, close: number): string {
  const top = Math.min(open, close), bottom = Math.max(open, close)
  const h = Math.max(1, bottom - top)
  const l = x - w / 2
  return `M${r2(x)},${r2(high)}V${r2(top)}M${r2(x)},${r2(top + h)}V${r2(Math.max(low, top + h))}M${r2(l)},${r2(top)}h${r2(w)}v${r2(h)}h${r2(-w)}Z`
}

export const candlestick = defineChart<CandleRow[], CandleOptions>({
  id: 'candlestick',
  name: 'Candlestick chart',
  category: 'trend',
  intents: ['trend', 'change-over-time'],
  summary: 'Open, high, low and close per session as candles, with optional volume.',
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
    const { theme } = ctx
    return drawOhlc(ctx, (g) => {
      const solid = !g.up || g.w < 3
      return {
        type: 'path', role: 'candle', key: g.key, d: candlePath(g.x, g.w, g.open, g.high, g.low, g.close),
        fill: solid ? g.color : theme.surface, stroke: g.color, strokeWidth: 1, pattern: g.up ? undefined : 0,
        cx: g.x, cy: (g.high + g.low) / 2, box: { x: g.x - g.w / 2 - 1, y: g.high, width: g.w + 2, height: Math.max(2, g.low - g.high) },
        datum: g.row, label: g.label, tooltip: g.tooltip,
      }
    })
  },
  describe: ({ data, options: o, fmt, name }) => describeOhlc(data, o, fmt, name),
  table: (data, o, fmt) => tableOhlc(data, o, fmt),
})

export default candlestick
