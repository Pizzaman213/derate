// Population pyramid: back-to-back horizontal bars per age band, one group on each side of a
// shared zero, on a symmetric value axis so equal values have equal lengths on both sides.

import { defineChart, phrase, SIZES, type Mark } from '../../core'
import { notes, uniq } from '../../core/helpers'
import { input, options, type PyramidOptions, type PyramidRow } from './schema'

const MALE = /^(m|male|males|men|man|boys?|hombres?|männer|homme?s?)$/i
const FEMALE = /^(f|female|females|women|woman|girls?|mujer(es)?|frauen|femmes?)$/i

interface Pyramid {
  left: string
  right: string
  /** Bands youngest first (or first-seen first when the labels carry no numbers). */
  ages: string[]
  value: Map<string, number>
  total: number
  sideTotal: [number, number]
  duplicates: number
  nulls: number
  negatives: number
  /** Rows whose group is neither side. */
  others: string[]
  otherRows: number
}

const cell = (age: string, side: string) => `${age}\u0000${side}`

/** Bands youngest first: by the first number in each label when every label has one, else as given. */
function youngestFirst(ages: string[]): string[] {
  const nums = ages.map((a) => a.match(/\d+(?:[.,]\d+)?/)?.[0])
  if (nums.some((n) => n === undefined)) return ages
  const v = nums.map((n) => Number(n!.replace(',', '.')))
  if (v.every((x, i) => i === 0 || x >= v[i - 1]!)) return ages
  if (v.every((x, i) => i === 0 || x <= v[i - 1]!)) return [...ages].reverse()
  return ages.map((a, i) => ({ a, n: v[i]! })).sort((p, q) => p.n - q.n).map((p) => p.a)
}

function pyramidOf(rows: PyramidRow[], o: PyramidOptions): Pyramid {
  const groups = uniq(rows.map((r) => r.sex))
  const find = (name: string | undefined) => (name === undefined ? undefined : groups.find((g) => g.toLowerCase() === name.toLowerCase()) ?? name)
  let left = find(o.left), right = find(o.right)
  const male = groups.find((g) => MALE.test(g.trim())), female = groups.find((g) => FEMALE.test(g.trim()))
  if (left === undefined && right === undefined && male !== undefined && female !== undefined) [left, right] = [male, female]
  // one side present: name the missing one after it ("male" -> "female")
  const lower = (g: string) => g === g.toLowerCase()
  if (left === undefined && right === undefined && groups.length === 1 && male !== undefined) [left, right] = [male, lower(male) ? 'female' : 'Female']
  if (left === undefined && right === undefined && groups.length === 1 && female !== undefined) [left, right] = [lower(female) ? 'male' : 'Male', female]
  left ??= groups.find((g) => g !== right) ?? 'Left'
  right ??= groups.find((g) => g !== left) ?? 'Right'
  const value = new Map<string, number>()
  const ages: string[] = []
  const seenAge = new Set<string>(), seenCell = new Set<string>()
  let duplicates = 0, nulls = 0, negatives = 0, otherRows = 0
  const others = new Set<string>()
  for (const r of rows) {
    if (r.sex !== left && r.sex !== right) { others.add(r.sex); otherRows++; continue }
    if (!seenAge.has(r.age)) { seenAge.add(r.age); ages.push(r.age) }
    const k = cell(r.age, r.sex)
    if (seenCell.has(k)) duplicates++
    seenCell.add(k)
    if (r.value === null) { nulls++; continue }
    if (r.value < 0) { negatives++; continue }
    value.set(k, (value.get(k) ?? 0) + r.value)
  }
  let l = 0, rt = 0
  for (const a of ages) { l += value.get(cell(a, left)) ?? 0; rt += value.get(cell(a, right)) ?? 0 }
  return { left, right, ages: youngestFirst(ages), value, total: l + rt, sideTotal: [l, rt], duplicates, nulls, negatives, others: [...others], otherRows }
}

export const populationPyramid = defineChart<PyramidRow[], PyramidOptions>({
  id: 'population-pyramid',
  name: 'Population pyramid',
  category: 'distribution',
  intents: ['distribution', 'compare'],
  summary: 'Back-to-back bars of two groups (usually male and female) across age bands.',
  input,
  options,
  shape: 'rows',
  fields: ['age', 'sex', 'value'],
  numberFields: ['value'],
  aspect: 0.8,
  maxHeight: 720,
  isEmpty(data, o) {
    if (data.length === 0) return true
    const p = pyramidOf(data, o)
    if (p.value.size === 0) return 'No values to plot'
    return p.total === 0 ? 'All values are zero' : false
  },
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const p = pyramidOf(ctx.data, o)
    const sides = [p.left, p.right] as const
    const shown = (v: number) => (o.percentages ? v / p.total : v)
    const show = (v: number) => (o.percentages ? fmt.pct(v, 1) : fmt.value(v))
    let max = 0
    for (const v of p.value.values()) max = Math.max(max, shown(v))
    if (max === 0) max = 1
    const domain = o.order === 'youngest-bottom' ? [...p.ages].reverse() : uniq(ctx.data.filter((r) => r.sex === p.left || r.sex === p.right).map((r) => r.age))
    const f = ctx.cartesian({
      x: { type: 'linear', domain: [-max, max] },
      y: { type: 'band', domain, padding: 0.12 },
      xFormat: (v: number) => (o.percentages ? fmt.pct(Math.abs(v)) : fmt.value(Math.abs(v))),
      xTitle: o.valueTitle,
      yTitle: o.ageTitle,
      grid: 'x',
      pad: { top: SIZES.label + 6 },
    })
    const x0 = f.px(0)
    const marks: Mark[] = [...f.marks]
    // side names over each half, so the reading direction is clear without the legend
    sides.forEach((s, i) => {
      const half = i === 0 ? x0 - f.plot.x : f.plot.x + f.plot.width - x0
      const text = ctx.truncate(s, Math.max(16, half - 8), SIZES.label)
      marks.push({ type: 'text', layer: 'labels', key: `side:${i}`, x: i === 0 ? x0 - 6 : x0 + 6, y: f.plot.y - 6, text, full: text !== s ? s : undefined, anchor: i === 0 ? 'end' : 'start', baseline: 'alphabetic', size: SIZES.label, weight: 600, fill: theme.inkSecondary })
    })
    for (const age of domain) {
      // a handful of bands would make slabs: cap the thickness and centre it in the band
      const bh = Math.max(0.5, Math.min(f.bh, 56))
      const y = f.py(age) + (f.bh - bh) / 2
      sides.forEach((side, i) => {
        const raw = p.value.get(cell(age, side))
        if (raw === undefined) return
        const v = shown(raw)
        const end = f.px(i === 0 ? -v : v)
        const width = Math.abs(end - x0)
        const fill = theme.cat(i)
        const share = p.total > 0 ? fmt.pct(raw / p.total, 1) : ''
        marks.push({
          type: 'rect', role: 'bar', key: `${i}:${age}`, series: side,
          x: raw === 0 ? (i === 0 ? x0 - 1 : x0) : Math.min(end, x0), y, width: raw === 0 ? 1 : width, height: bh, rx: Math.min(2, bh / 4),
          fill, pattern: i,
          datum: { age, sex: side, value: raw },
          label: `${age}, ${side}: ${show(v)}${o.percentages ? '' : ` (${share} of total)`}`,
          tooltip: { title: `${age} · ${side}`, rows: [['Value', fmt.value(raw)], ['Share of total', share]], color: fill },
        })
      })
    }
    return {
      marks,
      plot: f.plot,
      legend: { items: sides.map((s, i) => ({ key: s, label: s, color: theme.cat(i), pattern: i })) },
      note: notes([
        [p.duplicates, 'duplicate age/group rows summed'],
        [p.nulls, 'missing values left blank'],
        [p.negatives, 'negative values omitted'],
        [p.otherRows, `rows for ${phrase.listPhrase(p.others.map((g) => `'${g}'`))} omitted (a pyramid has two sides)`],
      ]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const p = pyramidOf(data, o)
    const share = (v: number) => (p.total > 0 ? fmt.pct(v / p.total) : fmt.value(v))
    let s = `${name}, ${p.left} and ${p.right} across ${phrase.count(p.ages.length, 'age band')}, total ${fmt.value(p.total)} (${p.left} ${share(p.sideTotal[0])}, ${p.right} ${share(p.sideTotal[1])})`
    let best: { age: string; side: string; v: number } | null = null
    for (const age of p.ages) for (const side of [p.left, p.right]) {
      const v = p.value.get(cell(age, side))
      if (v !== undefined && (!best || v > best.v)) best = { age, side, v }
    }
    if (best) s += `, largest ${best.age} ${best.side} at ${o.percentages ? share(best.v) : fmt.value(best.v)}`
    return s
  },
  table(data, o) {
    const p = pyramidOf(data, o)
    return {
      columns: ['Age', p.left, p.right],
      rows: p.ages.map((a) => [a, p.value.get(cell(a, p.left)) ?? null, p.value.get(cell(a, p.right)) ?? null]),
    }
  },
})

export default populationPyramid
