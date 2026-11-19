import { bin, extent, nice, thresholdFreedmanDiaconis, thresholdScott, thresholdSturges } from 'd3-array'

/** One histogram bin: [x0, x1) (the last bin closed), how many values fell in it, and which. */
export type Bin = { x0: number; x1: number; count: number; values: number[] }

/** Bin options: a target count, explicit thresholds, a fixed domain, or a rule for the count. */
export type BinOptions = {
  count?: number
  thresholds?: number[]
  domain?: [number, number]
  method?: 'sturges' | 'freedman-diaconis' | 'scott'
}

const METHODS = { sturges: thresholdSturges, 'freedman-diaconis': thresholdFreedmanDiaconis, scott: thresholdScott }

/** Histogram the finite values on a nice domain; counts sum to the finite inputs inside the domain. */
export function binValues(values: readonly (number | null | undefined)[], opts: BinOptions = {}): Bin[] {
  const xs = values.filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
  if (xs.length === 0) return []
  const [lo, hi] = opts.domain ?? (extent(xs) as [number, number])
  const target = opts.count ?? METHODS[opts.method ?? 'sturges'](xs, lo, hi)
  const exact = opts.domain !== undefined || opts.thresholds !== undefined || lo === hi
  const domain: [number, number] = exact ? [lo, hi] : (nice(lo, hi, target) as [number, number])
  const binner = bin<number, number>().domain(domain)
  if (opts.thresholds) binner.thresholds(opts.thresholds)
  else binner.thresholds(target)
  return binner(xs).map((b) => ({ x0: b.x0 ?? domain[0], x1: b.x1 ?? domain[1], count: b.length, values: [...b] }))
}
