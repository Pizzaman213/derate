import { describe, expect, it } from 'vitest'
import { chartSuite, host, loadSamples } from '../../tests/harness'
import { measure } from '../../core'
import { mulberry32 } from '../../core/stats'
import wordCloud, { countWords, spiralLayout } from './index'

chartSuite({
  module: wordCloud,
  dir: import.meta.dirname,
  role: 'word',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 20, 'edge-single': 1, 'edge-nulls': 10, 'edge-negatives': 10, 'edge-duplicates': 20, 'edge-large': 150, 'edge-grouped': 13 },
  summary: /^Word cloud, 20 words, highest latency at 96, lowest eviction at 5$/,
  malformed: [
    { name: 'a text value', data: [{ text: 'a', value: 1 }, { text: 'b', value: 'many' }], message: /field 'value' must be a number, got 'many' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of \{ text, value \} rows/ },
    { name: 'a mapping to a missing column', data: [{ word: 'a', n: 1 }], options: { fields: { text: 'word', value: 'count' } }, message: /column 'count' not found/ },
  ],
})

const overlaps = (a: { x: number; y: number; w: number; h: number }, b: { x: number; y: number; w: number; h: number }) =>
  Math.abs(a.x - b.x) * 2 < a.w + b.w - 1e-9 && Math.abs(a.y - b.y) * 2 < a.h + b.h - 1e-9

describe('word-cloud: layout', () => {
  for (const s of loadSamples(import.meta.dirname).filter((x) => ['basic', 'edge-rotated', 'edge-long-labels', 'edge-large'].includes(x.name))) {
    it(`${s.name}: no two word boxes overlap and every box is inside the frame`, () => {
      for (const [W, H] of [[640, 400], [320, 220], [1280, 600]] as const) {
        const el = host(W, H)
        const h = wordCloud.render(el, s.data, { animate: false, ...s.options, height: H })
        const svg = el.querySelector('svg.oc-svg')!
        const sw = Number(svg.getAttribute('width')), sh = Number(svg.getAttribute('height'))
        const boxes = [...svg.querySelectorAll('rect[data-role="word"]')].map((r) => {
          const x = Number(r.getAttribute('x')), y = Number(r.getAttribute('y')), w = Number(r.getAttribute('width')), hh = Number(r.getAttribute('height'))
          return { x: x + w / 2, y: y + hh / 2, w, h: hh }
        })
        expect(boxes.length).toBeGreaterThan(0)
        for (const b of boxes) {
          expect(b.x - b.w / 2).toBeGreaterThanOrEqual(-0.01)
          expect(b.y - b.h / 2).toBeGreaterThanOrEqual(-0.01)
          expect(b.x + b.w / 2).toBeLessThanOrEqual(sw + 0.01)
          expect(b.y + b.h / 2).toBeLessThanOrEqual(sh + 0.01)
        }
        for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) expect(overlaps(boxes[i]!, boxes[j]!)).toBe(false)
        h.destroy()
        el.remove()
      }
    })
  }
  it('spiralLayout: deterministic for a seed, and drops what cannot fit', () => {
    const words = Array.from({ length: 80 }, (_, i) => ({ width: measure(`word${i}`, 30 - i / 4), height: (30 - i / 4) * 1.2, rotate: i % 3 === 1 }))
    const a = spiralLayout(words, 300, 160, mulberry32(4))
    const b = spiralLayout(words, 300, 160, mulberry32(4))
    expect(a).toEqual(b)
    expect(a.some((x) => x === null)).toBe(true)
    const placed = a.filter((x): x is NonNullable<typeof x> => x !== null)
    for (let i = 0; i < placed.length; i++) for (let j = i + 1; j < placed.length; j++) expect(overlaps(placed[i]!, placed[j]!)).toBe(false)
    // a box wider than the frame is never placed
    expect(spiralLayout([{ width: 500, height: 20, rotate: false }], 300, 160, mulberry32(1))).toEqual([null])
  })
  it('the largest word is the biggest font, sizes follow a square-root scale', () => {
    const el = host(640, 400)
    const h = wordCloud.render(el, [{ text: 'big', value: 100 }, { text: 'mid', value: 25 }, { text: 'small', value: 1 }], { animate: false, minFontSize: 12, maxFontSize: 48 })
    const size = (t: string) => parseFloat([...el.querySelectorAll('text')].find((x) => x.textContent === t)!.getAttribute('style')!.match(/font-size:([\d.]+)px/)![1]!)
    expect(size('big')).toBeCloseTo(48, 5)
    expect(size('small')).toBeCloseTo(12, 5)
    // sqrt(25) is 4/9 of the way from sqrt(1) to sqrt(100)
    expect(size('mid')).toBeCloseTo(12 + (4 / 9) * 36, 5)
    h.destroy()
  })
  it('never sets a font below the tick size', () => {
    const el = host(640, 400)
    const h = wordCloud.render(el, [{ text: 'a1', value: 100 }, { text: 'b2', value: 1 }], { animate: false, minFontSize: 2, maxFontSize: 4 })
    for (const t of el.querySelectorAll('text')) expect(parseFloat(t.getAttribute('style')!.match(/font-size:([\d.]+)px/)![1]!)).toBeGreaterThanOrEqual(11)
    h.destroy()
  })
  it('counts prose: stopwords dropped, case folded to the commonest spelling', () => {
    const rows = countWords('The GPU and the gpu. GPU memory, memory and the Memory! Of it a 42', ['memory'])
    expect(rows).toEqual([{ text: 'GPU', value: 3, group: undefined }])
    const el = host()
    const h = wordCloud.render(el, 'Fast fast FAST. Slow, slow and cheap.', { animate: false })
    expect(h.exportCSV()).toBe('Word,Value\nfast,3\nslow,2\ncheap,1')
    h.update({ text: 'alpha beta beta' })
    expect(h.exportCSV()).toBe('Word,Value\nbeta,2\nalpha,1')
    h.destroy()
  })
  it('notes words that do not fit and words beyond maxWords', () => {
    const el = host(320, 200)
    const data = Array.from({ length: 300 }, (_, i) => ({ text: `term${i}`, value: 300 - i }))
    const h = wordCloud.render(el, data, { animate: false, height: 200, maxWords: 200 })
    const note = el.querySelector('.oc-note')!.textContent!
    expect(note).toMatch(/100 smaller words beyond the 200-word limit left out/)
    expect(note).toMatch(/\d+ words did not fit/)
    expect(h.exportCSV().split('\n').length).toBe(301)
    h.destroy()
  })
})
