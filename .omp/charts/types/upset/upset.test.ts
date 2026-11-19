import { describe, expect, it } from 'vitest'
import { chartSuite, host, loadSamples } from '../../tests/harness'
import upset, { intersections } from './index'

chartSuite({
  module: upset,
  dir: import.meta.dirname,
  role: 'bar',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 10, 'edge-single': 1, 'edge-aggregated': 7, 'edge-duplicates': 4, 'edge-degree': 10 },
  summary: /^UpSet plot, 4 sets, 10 intersections, largest Python and SQL only at 3, largest set Python at 9$/,
  malformed: [
    { name: 'a text size', data: [{ sets: 'A', size: 3 }, { sets: 'B', size: 'few' }], message: /field 'size' must be a number, got 'few' at row 2/ },
    { name: 'sets that are not text', data: [{ sets: [{ a: 1 }] }], message: /field 'sets\[0\]' must be a text, got an object at row 1/ },
    { name: 'a scalar', data: 5, message: /must be a list of \{ item\?, sets \} or \{ sets, size \} rows/ },
  ],
})

/** Brute force: every item's exact membership, counted. */
function bruteForce(rows: Array<{ item?: string; sets: string }>): Map<string, number> {
  const items = new Map<string, Set<string>>()
  rows.forEach((r, i) => {
    const id = r.item ?? `#${i}`
    const s = items.get(id) ?? new Set<string>()
    r.sets.split(/[,;|]/).map((x) => x.trim()).filter(Boolean).forEach((x) => s.add(x))
    items.set(id, s)
  })
  const out = new Map<string, number>()
  for (const s of items.values()) {
    if (!s.size) continue
    const k = [...s].sort().join('|')
    out.set(k, (out.get(k) ?? 0) + 1)
  }
  return out
}

describe('upset: intersections', () => {
  const samples = loadSamples(import.meta.dirname)
  it('exclusive intersection sizes match a brute-force count of memberships', () => {
    for (const name of ['basic', 'edge-duplicates', 'edge-unicode-rtl', 'edge-long-labels']) {
      const s = samples.find((x) => x.name === name)!
      const v = upset.validate(s.data, s.options)
      expect(v.ok).toBe(true)
      const m = intersections(v.data!, 'inclusive')
      const want = bruteForce(s.data as Array<{ item?: string; sets: string }>)
      expect(new Map(m.combos.map((c) => [[...c.sets].sort().join('|'), c.size]))).toEqual(want)
    }
  })
  it('counts the long-format CSV export exactly', () => {
    const s = samples.find((x) => x.name === 'realistic')!
    const csv = (s.data as string).trim().split('\n').slice(1).map((l) => { const [item, sets] = l.split(','); return { item: item!, sets: sets! } })
    const v = upset.validate(s.data, s.options)
    const m = intersections(v.data!, 'inclusive')
    expect(new Map(m.combos.map((c) => [[...c.sets].sort().join('|'), c.size]))).toEqual(bruteForce(csv))
    // set totals are the number of items in each set
    const accounts = new Map<string, Set<string>>()
    for (const r of csv) accounts.set(r.sets, (accounts.get(r.sets) ?? new Set<string>()).add(r.item))
    for (const set of m.sets) expect(set.size).toBe(accounts.get(set.name)!.size)
    // every account lands in exactly one intersection
    expect(m.combos.reduce((n, c) => n + c.size, 0)).toBe(new Set(csv.map((r) => r.item)).size)
  })
  it('inclusive aggregated sizes become exclusive by inclusion-exclusion', () => {
    const m = intersections(upset.validate([{ sets: 'A', size: 12 }, { sets: 'B', size: 12 }, { sets: 'C', size: 12 }, { sets: 'A,B', size: 4 }, { sets: 'A,C', size: 4 }, { sets: 'B,C', size: 4 }, { sets: 'A,B,C', size: 2 }]).data!, 'inclusive')
    const by = new Map(m.combos.map((c) => [c.sets.join('|'), c.size]))
    expect(by.get('A')).toBe(6)
    expect(by.get('A|B')).toBe(2)
    expect(by.get('A|B|C')).toBe(2)
    expect(m.sets.map((s) => s.size)).toEqual([12, 12, 12])
  })
  it('exclusive aggregated sizes are taken as given', () => {
    const m = intersections(upset.validate([{ sets: 'A', size: 5 }, { sets: 'A,B', size: 3 }, { sets: 'B,A', size: 1 }]).data!, 'exclusive')
    expect(m.combos.map((c) => [c.sets.join('|'), c.size])).toEqual([['A', 5], ['A|B', 4]])
    expect(m.counts.duplicates).toBe(1)
  })
})

describe('upset: layout', () => {
  it('bars are proportional to intersection size, sorted largest first', () => {
    const el = host(640, 420)
    const h = upset.render(el, [{ sets: 'A' }, { sets: 'A' }, { sets: 'A' }, { sets: 'A' }, { sets: 'A,B' }, { sets: 'A,B' }, { sets: 'B' }], { animate: false })
    const bars = [...el.querySelectorAll('rect[data-role="bar"]')]
    expect(bars.map((b) => b.getAttribute('data-key'))).toEqual(['rect:c:A', 'rect:c:A|B', 'rect:c:B'])
    const hs = bars.map((b) => Number(b.getAttribute('height')))
    expect(hs[0]! / hs[1]!).toBeCloseTo(2, 1)
    expect(hs[0]! / hs[2]!).toBeCloseTo(4, 1)
    // set bars: A has 6, B has 3
    const sets = [...el.querySelectorAll('rect[data-role="set"]')].map((r) => Number(r.getAttribute('width')))
    expect(sets[0]! / sets[1]!).toBeCloseTo(2, 1)
    h.destroy()
  })
  it('sortBy degree puts single sets first', () => {
    const el = host(640, 420)
    const h = upset.render(el, [{ sets: 'A,B' }, { sets: 'A,B' }, { sets: 'A,B' }, { sets: 'A' }, { sets: 'B' }, { sets: 'B' }], { animate: false, sortBy: 'degree' })
    expect([...el.querySelectorAll('rect[data-role="bar"]')].map((b) => b.getAttribute('data-key'))).toEqual(['rect:c:B', 'rect:c:A', 'rect:c:A|B'])
    h.destroy()
  })
  it('drops the smallest intersections when columns run out, says so, keeps them in the table', () => {
    const el = host(320, 300)
    const s = loadSamples(import.meta.dirname).find((x) => x.name === 'edge-large')!
    const h = upset.render(el, s.data, { animate: false, height: 300 })
    const m = intersections(upset.validate(s.data).data!, 'inclusive')
    const shown = el.querySelectorAll('rect[data-role="bar"]').length
    expect(shown).toBeLessThan(m.combos.length)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(new RegExp(`${m.combos.length - shown} smaller intersections not shown`))
    expect(h.exportCSV().trim().split('\n').length).toBe(m.combos.length + 1)
    h.destroy()
  })
  it('notes items in no set and repeated rows', () => {
    const el = host()
    const h = upset.render(el, [{ sets: 'A' }, { sets: '' }, { sets: 'B' }, { item: 'x', sets: 'A' }, { item: 'x', sets: 'A' }], { animate: false })
    const note = el.querySelector('.oc-note')!.textContent!
    expect(note).toMatch(/1 items in no set not shown/)
    expect(note).toMatch(/1 repeated rows ignored/)
    h.destroy()
  })
})
