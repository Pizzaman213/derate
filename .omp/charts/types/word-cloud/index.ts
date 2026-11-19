// Word cloud: words sized by value (square-root scale between the min and max font size),
// placed largest first along a deterministic Archimedean spiral from the centre, each at
// the first spot where its measured box collides with nothing already placed. Words that
// find no spot inside the frame are left out and counted in the note.
//
// d3-cloud is deliberately not used: it hit-tests glyph pixels on a canvas, which jsdom
// does not have, so layout would differ between the browser and the tests.

import { defineChart, phrase, SIZES } from '../../core'
import type { Mark } from '../../core'
import { notes, uniq } from '../../core/helpers'
import { mulberry32 } from '../../core/stats'
import { AdapterError, isRecord, looksLikeCSV, mapFields, parseCSV, toRows } from '../../adapters'
import { input, options, type WordCloudOptions, type WordRow } from './schema'

// ---------------------------------------------------------------------------------------
// Prose -> counted rows

const STOPWORDS = new Set((
  'a about above after again against all am an and any are as at be because been before being below between both but by ' +
  'can could did do does doing down during each few for from further had has have having he her here hers herself him himself ' +
  'his how i if in into is it its itself just me more most my myself no nor not now of off on once only or other our ours ' +
  'ourselves out over own same she should so some such than that the their theirs them themselves then there these they this ' +
  'those through to too under until up us very was we were what when where which while who whom why will with would you your ' +
  'yours yourself yourselves also may might must shall cannot every even many much let please really still yet ' +
  "s t don't can't won't isn't aren't wasn't weren't it's i'm i've you're we're they're"
).split(/\s+/))

/** Count the words of a text: lower-cased keys, the most common spelling shown, stopwords and 1-letter tokens dropped. */
export function countWords(text: string, extraStopwords: readonly string[] = []): WordRow[] {
  const stop = new Set([...STOPWORDS, ...extraStopwords.map((w) => w.toLowerCase())])
  const counts = new Map<string, { n: number; forms: Map<string, number> }>()
  for (const m of text.matchAll(/[\p{L}\p{N}][\p{L}\p{N}\p{M}'’-]*/gu)) {
    const form = m[0].replace(/['’-]+$/, '').replace(/’/g, "'")
    const key = form.toLocaleLowerCase()
    if ([...key].length < 2 || stop.has(key) || /^\p{N}+$/u.test(key)) continue
    const hit = counts.get(key) ?? { n: 0, forms: new Map<string, number>() }
    hit.n++
    hit.forms.set(form, (hit.forms.get(form) ?? 0) + 1)
    counts.set(key, hit)
  }
  return [...counts.entries()].map(([key, { n, forms }]) => {
    // the commonest spelling; on a tie the lower-case one, else the first seen
    let best = '', bestN = -1
    for (const [f, k] of forms) if (k > bestN || (k === bestN && f === key)) { best = f; bestN = k }
    return { text: best, value: n, group: undefined }
  })
}

function adapt(raw: unknown, opts: { fields?: Record<string, string>; stopwords?: unknown }): unknown {
  if (raw === null || raw === undefined) return []
  const extra = Array.isArray(opts.stopwords) ? opts.stopwords.filter((w): w is string => typeof w === 'string') : []
  const mapping = opts.fields && Object.keys(opts.fields).length ? opts.fields : undefined
  if (isRecord(raw) && typeof raw.text === 'string') return countWords(raw.text, extra)
  if (typeof raw === 'string') {
    // a table with a 'text' column (or a mapping) is rows; anything else is prose to count
    const rows = looksLikeCSV(raw) ? parseCSV(raw) : null
    if (rows && mapping) return mapFields(rows, mapping)
    if (rows && rows.length && Object.hasOwn(rows[0]!, 'text')) return rows
    return countWords(raw, extra)
  }
  const rows = toRows(raw)
  if (rows === null) throw new AdapterError('the data must be a list of { text, value } rows, CSV text, column arrays, prose, or { text: "…" }')
  return mapping ? mapFields(rows, mapping) : rows
}

// ---------------------------------------------------------------------------------------
// Layout

export interface WordBox {
  /** Centre. */
  x: number
  y: number
  /** Axis-aligned box size (already swapped when rotated). */
  w: number
  h: number
}
export interface WordSpec { width: number; height: number; rotate: boolean }

/** Box padding between words, px. */
const PAD = 2

/**
 * A packer for one frame: `place` puts a box (largest words first) on an Archimedean spiral
 * stretched to the frame's aspect, at the first point from the centre where it is inside the
 * frame and overlaps nothing placed, and returns it -- or null when no spot was found.
 *
 * Cost is spiral steps x collision tests, so: the spiral advances about a quarter of the
 * word's smaller side per step (a big word never needs 3 px steps), placed boxes sit in a
 * uniform grid so a test only visits nearby ones, and the box that blocked the previous step
 * is tried first (it usually blocks the next one too).
 */
function spiralPacker(W: number, H: number, random: () => number) {
  const cell = 24
  const cols = Math.max(1, Math.ceil(W / cell)), rows = Math.max(1, Math.ceil(H / cell))
  const grid: number[][] = Array.from({ length: cols * rows }, () => [])
  const placed: WordBox[] = []
  const clampC = (v: number) => Math.min(cols - 1, Math.max(0, Math.floor(v / cell)))
  const clampR = (v: number) => Math.min(rows - 1, Math.max(0, Math.floor(v / cell)))
  const overlap = (p: WordBox, x: number, y: number, w: number, h: number) => Math.abs(p.x - x) * 2 < p.w + w && Math.abs(p.y - y) * 2 < p.h + h
  /** Index of a placed box overlapping (x, y, w, h), or -1. */
  const hit = (x: number, y: number, w: number, h: number, last: number) => {
    if (last >= 0 && overlap(placed[last]!, x, y, w, h)) return last
    const c0 = clampC(x - w / 2), c1 = clampC(x + w / 2), r0 = clampR(y - h / 2), r1 = clampR(y + h / 2)
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) {
      for (const pi of grid[r * cols + c]!) if (overlap(placed[pi]!, x, y, w, h)) return pi
    }
    return -1
  }
  const e = W / Math.max(1, H)
  const cx = W / 2, cy = H / 2
  return (wd: WordSpec): WordBox | null => {
    const w = (wd.rotate ? wd.height : wd.width) + PAD * 2
    const h = (wd.rotate ? wd.width : wd.height) + PAD * 2
    if (w > W || h > H) return null
    const start = random() * Math.PI * 2
    const step = Math.max(3, Math.min(w, h) / 4)
    // the ellipse r = t covers the frame's corners once t passes the half-diagonal (in y units)
    const tMax = (H / 2) * Math.SQRT2 + Math.max(w, h)
    let last = -1
    for (let t = 0; t <= tMax; t += Math.min(0.5, step / Math.max(1, t))) {
      const x = cx + e * t * Math.cos(t + start)
      const y = cy + t * Math.sin(t + start)
      if (x - w / 2 < 0 || x + w / 2 > W || y - h / 2 < 0 || y + h / 2 > H) continue
      last = hit(x, y, w, h, last)
      if (last >= 0) continue
      const b: WordBox = { x, y, w, h }
      const id = placed.push(b) - 1
      for (let r = clampR(y - h / 2), r1 = clampR(y + h / 2); r <= r1; r++) for (let c = clampC(x - w / 2), c1 = clampC(x + w / 2); c <= c1; c++) grid[r * cols + c]!.push(id)
      return b
    }
    return null
  }
}

/** Place every word with one packer (see spiralPacker): the box per word, or null. */
export function spiralLayout(words: readonly WordSpec[], W: number, H: number, random: () => number): Array<WordBox | null> {
  const place = spiralPacker(W, H, random)
  return words.map(place)
}

// ---------------------------------------------------------------------------------------

interface Word { text: string; value: number; group?: string }
/** A word at one attempt's scale: font, measured (possibly truncated) text, box size. */
interface SizedWord extends WordSpec { wd: Word; size: number; weight: number; shown: string }

function collapse(rows: readonly WordRow[]) {
  const map = new Map<string, Word>()
  let duplicates = 0, nulls = 0, nonPositive = 0
  for (const r of rows) {
    if (r.value === null) { nulls++; continue }
    if (r.value <= 0) { nonPositive++; continue }
    const hit = map.get(r.text)
    if (hit) { duplicates++; hit.value += r.value } else map.set(r.text, { text: r.text, value: r.value, group: r.group })
  }
  const words = [...map.values()].sort((a, b) => b.value - a.value || (a.text < b.text ? -1 : a.text > b.text ? 1 : 0))
  return { words, duplicates, nulls, nonPositive }
}

export const wordCloud = defineChart<WordRow[], WordCloudOptions>({
  id: 'word-cloud',
  name: 'Word cloud',
  category: 'sets',
  intents: ['compare', 'distribution'],
  summary: 'Words sized by frequency or weight, packed around the centre.',
  input,
  options,
  shape: 'object',
  fields: ['text', 'value', 'group'],
  numberFields: ['value'],
  aspect: 0.6,
  minHeight: 200,
  adapt: (raw, opts) => adapt(raw, opts),
  isEmpty(data) {
    if (!data.length) return true
    if (data.every((r) => r.value === null)) return 'No values to size the words by'
    if (data.every((r) => (r.value ?? 0) <= 0)) return 'All values are zero'
    return false
  },
  draw(ctx) {
    const { theme, options: o, fmt, width: W, height: H } = ctx
    const { words: all, duplicates, nulls, nonPositive } = collapse(ctx.data)
    const groups = uniq(all.flatMap((w) => (w.group === undefined ? [] : [w.group])))
    const visible = all.filter((w) => w.group === undefined || !ctx.hidden.has(w.group))
    const kept = visible.slice(0, o.maxWords)
    const capped = visible.length - kept.length

    const minF = Math.max(SIZES.tick, o.minFontSize ?? SIZES.tick)
    const lo = Math.sqrt(kept[kept.length - 1]?.value ?? 0), hi = Math.sqrt(kept[0]?.value ?? 0)
    // position of each word on the square-root scale, 0 (smallest) .. 1 (largest)
    const ts = kept.map((wd) => (hi - lo < 1e-12 ? 0.5 : (Math.sqrt(wd.value) - lo) / (hi - lo)))
    const lineH = 1.18
    // Without a maxFontSize, the largest size is the one at which the words' boxes cover
    // about 40% of the frame (text is ~linear in font size, so measure once at 100px),
    // never so large that the top word is wider than the frame.
    let maxF: number
    if (o.maxFontSize !== undefined) maxF = Math.max(minF, o.maxFontSize)
    else {
      const unit = kept.map((wd) => ctx.measure(wd.text, 100, 600) / 100)
      const area = (m: number) => kept.reduce((sum, _, i) => sum + unit[i]! * (minF + ts[i]! * (m - minF)) ** 2 * lineH, 0)
      let a = minF, b = Math.max(minF, Math.min(H / 2.5, (0.9 * W) / Math.max(1e-6, unit[0] ?? 1)))
      for (let k = 0; k < 30; k++) { const m = (a + b) / 2; if (area(m) > 0.4 * W * H) b = m; else a = m }
      maxF = a
    }
    const seed = Math.floor(ctx.random() * 2 ** 31)

    // lay out; when more than 5% of the words find no room, shrink the scale and retry.
    // Words are measured as they are placed, so an attempt that is going to be retried
    // stops -- measuring and packing nothing more -- as soon as it is over the 5%.
    const tolerated = 0.05 * kept.length
    const attempt = (top: number, final: boolean) => {
      const rnd = mulberry32(seed)
      const place = spiralPacker(W, H, rnd)
      const out: Array<{ s: SizedWord; b: WordBox | null }> = []
      let misses = 0
      for (let i = 0; i < kept.length && (final || misses <= tolerated); i++) {
        const wd = kept[i]!
        // half-pixel steps: invisible on screen, and every distinct size is a font the
        // browser has to instantiate before it can measure -- 150 words would be 150 fonts
        const size = Math.round((minF + ts[i]! * (top - minF)) * 2) / 2
        const weight = size >= (minF + top) / 2 && top > minF ? 600 : 400
        const rotate = o.rotate === 'mixed' && i > 0 && rnd() < 1 / 3
        // ctx.truncate measures regular weight; leave room for bold
        const room = ((rotate ? H : W) - PAD * 2 - 2) / (weight === 600 ? 1.08 : 1)
        const shown = ctx.truncate(wd.text, room, size)
        const s = { wd, size, weight, rotate, shown, width: ctx.measure(shown, size, weight), height: size * lineH }
        const b = place(s)
        if (!b) misses++
        out.push({ s, b })
      }
      return { layout: out, misses }
    }
    const RETRIES = 4
    const fixed = o.maxFontSize !== undefined
    let { layout, misses } = attempt(maxF, fixed || maxF <= minF)
    for (let k = 0; k < RETRIES && !fixed && maxF > minF && misses > tolerated; k++) {
      maxF = Math.max(minF, maxF * 0.85)
      ;({ layout, misses } = attempt(maxF, k === RETRIES - 1 || maxF <= minF))
    }

    const marks: Mark[] = []
    let dropped = 0
    layout.forEach(({ s, b }, i) => {
      if (!b || !s.shown) { dropped++; return }
      const gi = s.wd.group === undefined ? -1 : groups.indexOf(s.wd.group)
      const fill = gi >= 0 ? theme.cat(gi) : o.colorBy === 'single' ? theme.cat(0) : theme.cat(i)
      const label = `${s.wd.text}: ${fmt.value(s.wd.value)}`
      // the box is the data mark: hover, focus ring and click cover the whole word
      marks.push({
        type: 'rect', role: 'word', key: `w:${s.wd.text}`, x: b.x - b.w / 2, y: b.y - b.h / 2, width: b.w, height: b.h,
        fill: theme.surface, fillOpacity: 0, series: s.wd.group, datum: s.wd, label,
        tooltip: { title: s.wd.text, rows: [['Value', fmt.value(s.wd.value)], ...(s.wd.group !== undefined ? [['Group', s.wd.group] as [string, string]] : [])], color: fill },
      })
      marks.push({
        type: 'text', layer: 'labels', key: `t:${s.wd.text}`, x: b.x, y: b.y, text: s.shown, full: s.shown !== s.wd.text ? s.wd.text : undefined,
        anchor: 'middle', baseline: 'central', size: s.size, weight: s.weight, rotate: s.rotate ? -90 : undefined, fill, series: s.wd.group, inert: true,
      })
    })

    return {
      marks,
      legend: groups.length > 1 ? { items: groups.map((g, i) => ({ key: g, label: g, color: theme.cat(i), shape: 'square' as const })) } : undefined,
      note: notes([
        [duplicates, 'duplicate words summed'],
        [nulls, 'words without a value left out'],
        [nonPositive, 'words with a zero or negative value left out'],
        [capped, `smaller words beyond the ${o.maxWords}-word limit left out`],
        [dropped, 'words did not fit and were left out (all are in the table)'],
      ]),
    }
  },
  describe({ data, fmt, name }) {
    const { words } = collapse(data)
    return `${name}, ${phrase.count(words.length, 'word')}, ${phrase.extremes(words.map((w) => ({ label: w.text, value: w.value })), fmt)}`
  },
  table(data) {
    const { words } = collapse(data)
    const grouped = words.some((w) => w.group !== undefined)
    return grouped
      ? { columns: ['Word', 'Value', 'Group'], rows: words.map((w) => [w.text, w.value, w.group ?? null]) }
      : { columns: ['Word', 'Value'], rows: words.map((w) => [w.text, w.value]) }
  },
})

export default wordCloud
