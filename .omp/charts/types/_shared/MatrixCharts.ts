// Shared by the matrix charts (heatmap, adjacency-matrix, punch-card, table-heatmap):
// a continuous colour scale over the theme ramps, contrast-aware ink for text printed
// on a ramp colour, hatching for missing cells, and band padding that keeps 1-2 px gaps.

import { phrase, type Formatters, type LegendSpec, type Paint, type Palette } from '../../core'
import type { Labeled } from '../../core/describe'
import { finiteExtent } from '../../core/helpers'

export type ColorTransform = 'linear' | 'log'

export interface ColorScale {
  /** Ramp position in [0, 1] (0.5 is the neutral midpoint of a diverging ramp). */
  t(v: number): number
  paint(v: number): Paint
  diverging: boolean
  /** Domain ends shown in the legend (diverging: symmetric around the centre). */
  lo: number
  hi: number
  legend(fmt: Formatters, title?: string): NonNullable<LegendSpec['gradient']>
}

/** Signed log: monotone, defined at 0 and for negatives, so outliers compress without dropping anything. */
const slog = (v: number) => Math.sign(v) * Math.log10(1 + Math.abs(v))

/**
 * A sequential scale, or a diverging one around `center` when `diverging` is true or
 * (when 'auto') the values span the centre. `floor` lifts the sequential low end off the
 * ramp's first step, for charts whose "nothing" cells sit next to the smallest values.
 */
export function colorScale(theme: Palette, values: Iterable<number | null | undefined>, opts: { diverging?: boolean | 'auto'; center?: number; transform?: ColorTransform; floor?: number } = {}): ColorScale {
  const ext = finiteExtent(values) ?? [0, 0]
  const center = opts.center ?? 0
  const f = opts.transform === 'log' ? slog : (v: number) => v
  const div = opts.diverging === true || ((opts.diverging ?? 'auto') === 'auto' && ext[0] < center && ext[1] > center)
  if (div) {
    const reach = Math.max(Math.abs(ext[0] - center), Math.abs(ext[1] - center))
    const fr = Math.abs(f(center + reach) - f(center)) || 1
    const t = (v: number) => clamp01(0.5 + (f(v) - f(center)) / (2 * fr))
    return {
      t, diverging: true, lo: center - reach, hi: center + reach,
      paint: (v) => theme.div(t(v)),
      legend: (fmt) => ({ stops: theme.divStops(), min: fmt.value(center - reach), max: fmt.value(center + reach), mid: fmt.value(center) }),
    }
  }
  const [a, b] = ext
  const fa = f(a), span = f(b) - fa
  const floor = clamp01(opts.floor ?? 0)
  const t = (v: number) => floor + (1 - floor) * (span > 0 ? clamp01((f(v) - fa) / span) : 0.5)
  const stops = floor > 0 ? Array.from({ length: 9 }, (_, i) => theme.seq(floor + ((1 - floor) * i) / 8)) : theme.seqStops()
  return {
    t, diverging: false, lo: a, hi: b,
    paint: (v) => theme.seq(t(v)),
    legend: (fmt) => ({ stops, min: fmt.value(a), max: fmt.value(b) }),
  }
}

const clamp01 = (t: number) => (Number.isFinite(t) ? Math.min(1, Math.max(0, t)) : 0)

/**
 * Text colour for a cell at ramp position t. Every theme's ramps start near the surface
 * (sequential low end, diverging midpoint) and move away from it, so the far end takes
 * surface-coloured text and the near end takes ink.
 */
export function inkOn(theme: Palette, t: number, diverging: boolean): Paint {
  const distance = diverging ? Math.abs(t - 0.5) * 2 : t
  return distance > 0.55 ? theme.surface : theme.ink
}

/** 45-degree hatch lines clipped to a rectangle, as one path. */
export function hatchPath(x: number, y: number, w: number, h: number, step = 5): string {
  if (w <= 0 || h <= 0) return ''
  const parts: string[] = []
  const r = (n: number) => Math.round(n * 100) / 100
  // lines x' + y' = k for k in (0, w + h), in rect-local coordinates
  for (let k = step / 2; k < w + h; k += step) {
    const x0 = Math.min(k, w), y0 = k - x0 // top or right edge
    const y1 = Math.min(k, h), x1 = k - y1 // left or bottom edge
    parts.push(`M${r(x + x0)},${r(y + y0)}L${r(x + x1)},${r(y + y1)}`)
  }
  return parts.join('')
}

/** Band padding (fraction of a step) that leaves a gap of about 2 px, none when cells are tiny. */
export function bandPadding(extent: number, n: number, gap = 2): number {
  const step = extent / Math.max(1, n)
  if (step < 8) return 0
  return Math.min(0.12, gap / step)
}

/** Ordering of category names. */
export function orderNames(names: string[], order: 'input' | 'name' | 'value', total?: (name: string) => number): string[] {
  if (order === 'input') return names
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })
  if (order === 'name') return [...names].sort((a, b) => collator.compare(a, b))
  const sum = total ?? (() => 0)
  return [...names].sort((a, b) => sum(b) - sum(a) || collator.compare(a, b))
}

/** phrase.extremes, but "every cell at 0" when all values are equal (a highest and lowest that are the same cell reads as a bug). */
export function extremesOf(items: Labeled[], fmt: Formatters, noun: string): string {
  const ext = finiteExtent(items.map((d) => d.value))
  const n = items.filter((d) => typeof d.value === 'number' && Number.isFinite(d.value)).length
  if (ext && n > 1 && ext[0] === ext[1]) return `every ${noun} at ${fmt.value(ext[0])}`
  return phrase.extremes(items, fmt)
}
