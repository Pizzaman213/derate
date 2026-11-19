// Correlation matrix: one cell per pair of variables, coloured on a diverging ramp from
// -1 to +1, the coefficient printed where the cell is big enough. Takes wide rows (the
// coefficients are computed here, Pearson or Spearman) or a ready { names, matrix }.

import { defineChart, SIZES, stats, type Mark } from '../../core'
import { AdapterError, isRecord, toRows } from '../../adapters'
import { cellNum, fixed, numericColumns, strength } from '../_shared/RelationshipB'
import { input, options, type CorrData, type CorrOptions } from './schema'

const METHOD_NAME = { pearson: 'Pearson', spearman: 'Spearman', given: 'given' } as const

/** Wide rows / CSV / column arrays -> coefficients; { names, matrix } is checked for squareness. */
function adapt(raw: unknown, o: { method?: unknown; columns?: unknown }): unknown {
  if (isRecord(raw) && 'matrix' in raw) {
    const { names, matrix } = raw
    if (!Array.isArray(names)) throw new AdapterError(`'names' must be a list of variable names alongside 'matrix'`)
    if (!Array.isArray(matrix)) throw new AdapterError(`'matrix' must be a list of rows`)
    if (matrix.length !== names.length) throw new AdapterError(`'matrix' has ${matrix.length} rows but there are ${names.length} names`)
    matrix.forEach((row, i) => {
      if (!Array.isArray(row) || row.length !== names.length) throw new AdapterError(`'matrix' row ${i + 1} has ${Array.isArray(row) ? row.length : 0} values but there are ${names.length} names (the matrix must be square)`)
    })
    return { names: names.map((n) => (typeof n === 'string' ? n : String(n))), matrix, method: 'given', rows: null, missing: 0, ignored: [] }
  }
  const rows = raw === null || raw === undefined ? [] : toRows(raw)
  if (rows === null) throw new AdapterError(`the data must be rows of numeric columns, CSV text, column arrays, or { names, matrix }`)
  const pick = Array.isArray(o.columns) ? o.columns.filter((c): c is string => typeof c === 'string') : undefined
  const { columns, ignored } = numericColumns(rows, [], pick)
  const method = o.method === 'spearman' ? 'spearman' : 'pearson'
  const cols: Record<string, Array<number | null>> = {}
  let missing = 0
  for (const c of columns) {
    cols[c] = rows.map((r) => {
      const v = cellNum(r, c)
      if (v === null) missing++
      return v
    })
  }
  const { names, matrix } = stats.correlationMatrix(cols, method)
  // float error can land a hair outside [-1, 1]
  const m = matrix.map((row) => row.map((v) => (v === null ? null : Math.max(-1, Math.min(1, v)))))
  return { names, matrix: m, method, rows: rows.length, missing, ignored }
}

interface Cell { a: string; b: string; i: number; j: number; r: number | null }

function cellsOf(d: CorrData, triangle: 'full' | 'lower'): Cell[] {
  const out: Cell[] = []
  d.names.forEach((a, i) => d.names.forEach((b, j) => {
    if (triangle === 'lower' && j > i) return
    out.push({ a, b, i, j, r: d.matrix[i]?.[j] ?? null })
  }))
  return out
}

/** Strongest off-diagonal pairs (each pair once). */
function extremesOf(d: CorrData): { pos: Cell | null; neg: Cell | null; undefinedPairs: number } {
  let pos: Cell | null = null, neg: Cell | null = null, undefinedPairs = 0
  for (const c of cellsOf(d, 'lower')) {
    if (c.i === c.j) continue
    if (c.r === null) { undefinedPairs++; continue }
    if (c.r > 0 && (!pos || c.r > pos.r!)) pos = c
    if (c.r < 0 && (!neg || c.r < neg.r!)) neg = c
  }
  return { pos, neg, undefinedPairs }
}

export const correlationMatrix = defineChart<CorrData, CorrOptions>({
  id: 'correlation-matrix',
  name: 'Correlation matrix',
  category: 'relationship',
  intents: ['relationship', 'correlation'],
  summary: 'Pairwise correlation coefficients between numeric variables on a diverging −1…+1 scale.',
  input,
  options,
  shape: 'object',
  adapt,
  aspect: 0.8,
  minHeight: 220,
  maxHeight: 720,
  isEmpty: (d) => (d.names.length === 0 ? 'No numeric columns to correlate' : false),
  draw(ctx) {
    const { theme, options: o, fmt, data: d } = ctx
    const W = ctx.width, H = ctx.height
    const FS = SIZES.tick
    const k = d.names.length
    const nameW = Math.max(...d.names.map((n) => ctx.measure(n)))
    // row labels on the left, column labels under the grid (rotated when they do not fit)
    const left = Math.ceil(Math.min(nameW, W * 0.3, 160)) + 8
    const bottomRot = Math.ceil(Math.min(nameW, H * 0.28, 140) * Math.SQRT1_2 + FS * Math.SQRT1_2) + 8
    // the first row label is centred on a cell that may be thinner than the text is tall
    const top = Math.ceil(FS * 0.7) + 1
    let s = Math.max(1, Math.min((W - left - 8) / k, (H - top - (FS + 10)) / k))
    const horizontal = d.names.every((n) => ctx.measure(n) <= s - 4)
    const bottom = horizontal ? FS + 10 : bottomRot
    s = Math.max(1, Math.min((W - left - 8) / k, (H - top - bottom) / k))
    const x0 = left + Math.max(0, (W - 8 - left - k * s) / 2), y0 = Math.max(4, top - s / 2)
    const gap = s >= 12 ? 1 : 0
    const step = Math.max(1, Math.ceil((FS * 1.4) / s))
    const marks: Mark[] = []
    const valueW = ctx.measure('−0.00')
    const showVal = o.showValues && s - gap >= valueW + 6 && s - gap >= FS + 6
    const n = d.rows

    for (const c of cellsOf(d, o.triangle)) {
      const x = x0 + c.j * s + gap / 2, y = y0 + c.i * s + gap / 2, size = Math.max(0.5, s - gap)
      const r = c.r
      const fill = r === null ? theme.track : theme.div((r + 1) / 2)
      const rText = r === null ? 'not defined' : fixed(r, 2, fmt.locale)
      const pair = c.i === c.j ? c.a : `${c.a} and ${c.b}`
      const label = r === null
        ? `${pair}: correlation not defined (a column has no variation or too few shared rows)`
        : c.i === c.j ? `${c.a} with itself: ${rText}` : `${pair}: ${rText}, ${strength(r)}`
      const rows: Array<[string, string]> = [['Correlation', rText]]
      if (r !== null && c.i !== c.j) rows.push(['Strength', strength(r)])
      rows.push(['Method', METHOD_NAME[d.method]])
      if (n !== null) rows.push(['Rows', fmt.integer(n)])
      marks.push({
        type: 'rect', role: 'cell', key: `c:${c.a}×${c.b}`, x, y, width: size, height: size, rx: s >= 16 ? 2 : 0, fill,
        datum: { x: c.b, y: c.a, value: r }, label,
        tooltip: { title: c.i === c.j ? c.a : `${c.a} × ${c.b}`, rows, color: fill },
      })
      if (showVal) {
        marks.push({
          type: 'text', layer: 'labels', key: `v:${c.a}×${c.b}`, x: x + size / 2, y: y + size / 2, text: r === null ? '–' : rText,
          anchor: 'middle', baseline: 'central', size: FS, fill: r !== null && Math.abs(r) >= 0.6 ? theme.surface : theme.ink, inert: true,
        })
      }
    }
    // labels
    const rowMax = left - 8
    d.names.forEach((name, i) => {
      if (i % step !== 0) return
      const t = ctx.truncate(name, rowMax)
      marks.push({ type: 'text', layer: 'axes', key: `rl:${name}`, x: x0 - 6, y: y0 + i * s + s / 2, text: t, full: t !== name ? name : undefined, anchor: 'end', baseline: 'central', size: FS, fill: theme.inkSecondary, className: t !== name ? 'oc-trunc' : undefined })
    })
    const gridBottom = y0 + k * s
    d.names.forEach((name, j) => {
      if (j % step !== 0) return
      const cx = x0 + j * s + s / 2
      if (horizontal) {
        marks.push({ type: 'text', layer: 'axes', key: `cl:${name}`, x: cx, y: gridBottom + 6, text: name, anchor: 'middle', baseline: 'hanging', size: FS, fill: theme.inkSecondary })
      } else {
        // the label runs down-left from the cell; keep it inside the left edge and the budget
        const budget = Math.max(16, Math.min((bottom - 8 - FS * Math.SQRT1_2) / Math.SQRT1_2, (cx - 2) / Math.SQRT1_2))
        const t = ctx.truncate(name, budget)
        marks.push({ type: 'text', layer: 'axes', key: `cl:${name}`, x: cx, y: gridBottom + 6, text: t, full: t !== name ? name : undefined, anchor: 'end', baseline: 'central', rotate: -45, size: FS, fill: theme.inkSecondary, className: t !== name ? 'oc-trunc' : undefined })
      }
    })

    const { undefinedPairs } = extremesOf(d)
    const parts: string[] = []
    if (d.missing > 0) parts.push(`${d.missing} missing values; each pair uses the rows where both are present`)
    if (undefinedPairs > 0) parts.push(`${undefinedPairs} ${undefinedPairs === 1 ? 'pair' : 'pairs'} not defined (no variation)`)
    if (d.ignored.length) parts.push(`${d.ignored.length} non-numeric ${d.ignored.length === 1 ? 'column' : 'columns'} ignored`)
    if (step > 1) parts.push('some labels hidden for space; hover a cell for its pair')
    return {
      marks,
      legend: { title: METHOD_NAME[d.method] === 'given' ? 'Correlation' : `${METHOD_NAME[d.method]} r`, gradient: { stops: theme.divStops(), min: '−1', mid: '0', max: '+1' } },
      note: parts.length ? parts.join('; ') : undefined,
      plot: { x: x0, y: y0, width: k * s, height: k * s },
    }
  },
  describe({ data: d, fmt, name }) {
    const k = d.names.length
    const how = d.method === 'given' ? '' : ` (${METHOD_NAME[d.method]}${d.rows !== null ? `, ${d.rows} ${d.rows === 1 ? 'row' : 'rows'}` : ''})`
    const head = `${name}, ${k} ${k === 1 ? 'variable' : 'variables'}${how}`
    if (k < 2) return `${head}, no pairs to compare`
    const { pos, neg, undefinedPairs } = extremesOf(d)
    const bits: string[] = []
    if (pos) bits.push(`strongest positive ${pos.a} and ${pos.b} at ${fixed(pos.r!, 2, fmt.locale)}`)
    if (neg) bits.push(`strongest negative ${neg.a} and ${neg.b} at ${fixed(neg.r!, 2, fmt.locale)}`)
    if (!pos && !neg) bits.push(undefinedPairs ? 'no correlation is defined' : 'no correlation between any pair')
    return `${head}, ${bits.join(', ')}`
  },
  table(d) {
    return { columns: ['Variable', ...d.names], rows: d.names.map((a, i) => [a, ...d.names.map((_, j) => d.matrix[i]?.[j] ?? null)]) }
  },
})

export default correlationMatrix
