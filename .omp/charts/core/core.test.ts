import { describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { makeFormatters, tickGranularity } from './format'
import { palette, resolvePaint, effectiveTheme } from './theme'
import { measure, truncate, wrap } from './text'
import { cartesian, timeTicks } from './cartesian'
import { prepare } from './validate'
import { defineChart } from './define'
import { fields } from './index'
import { host } from '../tests/harness'
import type { Mark } from './types'

const fmt = makeFormatters()
const env = (w = 640, h = 400) => ({ width: w, height: h, fmt, theme: palette, measure: (t: string, s?: number) => measure(t, s), truncate: (t: string, x: number, s?: number) => truncate(t, x, s), zoom: null, zoomAxes: undefined })

describe('format', () => {
  it('SI suffixes', () => {
    expect(fmt.si(1200)).toBe('1.2k')
    expect(fmt.si(3_400_000)).toBe('3.4M')
    expect(fmt.si(2_500_000_000)).toBe('2.5B')
    expect(fmt.si(48_000)).toBe('48k')
    expect(fmt.si(-1500)).toBe('-1.5k')
    expect(fmt.si(999)).toBe('999')
    expect(fmt.si(null)).toBe('–')
  })
  it('is locale-aware', () => {
    const de = makeFormatters({ locale: 'de-DE' })
    expect(de.si(1200)).toBe('1,2k')
    expect(de.num(1234.5)).toBe('1.234,5')
    expect(makeFormatters({ locale: 'en-US', currency: 'EUR' }).currency(1234.5)).toBe('€1,235')
    expect(fmt.currency(2_400_000, true)).toBe('$2.4M')
    expect(fmt.pct(0.256)).toBe('26%')
  })
  it('value formats', () => {
    expect(makeFormatters({ valueFormat: '.2f' }).value(3.14159)).toBe('3.14')
    expect(makeFormatters({ valueFormat: 'integer' }).value(3.6)).toBe('4')
    expect(fmt.value(52_000)).toBe('52k')
  })
  it('dates in a time zone', () => {
    const d = new Date('2026-03-01T03:00:00Z')
    expect(makeFormatters({ timeZone: 'America/New_York' }).date(d, 'day')).toBe('Feb 28')
    expect(fmt.date(d, 'day')).toBe('Mar 1')
    expect(fmt.date(d, 'quarter')).toBe('Q1 2026')
    expect(fmt.date(d, 'month')).toBe('Mar 2026')
    expect(tickGranularity(365 * 864e5, 12)).toBe('month')
  })
})

describe('theme', () => {
  it('categorical colours wrap and resolve per theme', () => {
    expect(palette.cat(0)).toBe('var(--oc-cat-1)')
    expect(palette.cat(8)).toBe('var(--oc-cat-1)')
    expect(resolvePaint(palette.cat(0), 'light')).toBe('#004D7A')
    expect(resolvePaint(palette.cat(0), 'dark')).toBe('#1C99E8')
  })
  it('ramps interpolate between steps', () => {
    expect(palette.seq(0)).toBe('var(--oc-seq-0)')
    expect(palette.seq(1)).toBe('var(--oc-seq-8)')
    expect(palette.seq(0.0625)).toMatch(/^color-mix\(in oklab, var\(--oc-seq-0\) 50%, var\(--oc-seq-1\)\)$/)
    const mid = resolvePaint(palette.seq(0.0625), 'light')
    expect(mid).toMatch(/^#[0-9A-F]{6}$/)
    expect(palette.div(0.5)).toBe('var(--oc-div-5)')
  })
  it('effective theme follows the nearest data-theme', () => {
    const outer = document.createElement('div')
    outer.setAttribute('data-theme', 'dark')
    const inner = document.createElement('span')
    outer.appendChild(inner)
    expect(effectiveTheme(inner)).toBe('dark')
    expect(effectiveTheme(document.createElement('div'))).toBe('light')
  })
})

describe('text', () => {
  it('truncate fits the width and keeps short text', () => {
    expect(truncate('short', 200)).toBe('short')
    const t = truncate('a very long label that will not fit anywhere', 60)
    expect(t.endsWith('…')).toBe(true)
    expect(measure(t)).toBeLessThanOrEqual(60)
    expect(wrap('one two three four five six', 40, 2)).toHaveLength(2)
  })
})

describe('cartesian', () => {
  it('keeps the plot inside the box and labels apart', () => {
    const labels = Array.from({ length: 40 }, (_, i) => `Category number ${i}`)
    const f = cartesian(env(320, 240), { x: { type: 'band', domain: labels }, y: { type: 'linear', domain: [0, 1234567] } })
    expect(f.plot.x).toBeGreaterThan(0)
    expect(f.plot.x + f.plot.width).toBeLessThanOrEqual(320)
    expect(f.plot.y + f.plot.height).toBeLessThanOrEqual(240)
    const xl = f.marks.filter((m): m is Extract<Mark, { type: 'text' }> => m.type === 'text' && (m.key ?? '').startsWith('xl:'))
    expect(xl.length).toBeLessThan(40) // thinned
    expect(xl.every((m) => m.rotate === -45)).toBe(true)
  })
  it('y ticks are nice and within the domain', () => {
    const f = cartesian(env(), { x: { type: 'band', domain: ['a'] }, y: { type: 'linear', domain: [0, 97] } })
    const ys = f.yTicks as number[]
    expect(ys[0]).toBe(0)
    expect(ys[ys.length - 1]).toBe(100)
  })
  it('time ticks fall on local midnights in the chart time zone', () => {
    const { ticks, g } = timeTicks([new Date('2026-03-01T05:00:00Z'), new Date('2026-03-15T05:00:00Z')], 7, 'America/New_York')
    expect(g).toBe('day')
    for (const t of ticks) expect(new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: 'numeric', hourCycle: 'h23' }).format(t)).toBe('00')
    // spans the DST change on 2026-03-08 and still lands on midnights
    expect(ticks.some((t) => +t > Date.UTC(2026, 2, 8, 12))).toBe(true)
  })
  it('zoom rescales the x domain', () => {
    const e = { ...env(), zoom: { k: 2, x: 0, y: 0 } }
    const f0 = cartesian(env(), { x: { type: 'linear', domain: [0, 100] }, y: { type: 'linear', domain: [0, 1] }, zoom: true })
    const f2 = cartesian(e, { x: { type: 'linear', domain: [0, 100] }, y: { type: 'linear', domain: [0, 1] }, zoom: true })
    const d0 = (f0.x.domain() as number[]), d2 = (f2.x.domain() as number[])
    expect(d2[1]! - d2[0]!).toBeLessThan(d0[1]! - d0[0]!)
  })
})

describe('validate', () => {
  const schema = fields.rows({ label: fields.label(), value: fields.num(), when: fields.date() })
  it('accepts rows, CSV and column arrays alike', () => {
    const rows = [{ label: 'a', value: 1, when: '2026-01-01' }]
    expect(prepare(rows, schema, 'rows', undefined).ok).toBe(true)
    expect(prepare('label,value,when\na,1,2026-01-01', schema, 'rows', undefined).ok).toBe(true)
    expect(prepare({ label: ['a'], value: [1], when: ['2026-01-01'] }, schema, 'rows', undefined).ok).toBe(true)
    const mapped = prepare([{ name: 'a', amount: '1,234', day: '2026-01-01' }], schema, 'rows', { label: 'name', value: 'amount', when: 'day' })
    expect(mapped.ok).toBe(true)
    expect((mapped.data as Array<{ value: number }>)[0]!.value).toBe(1234)
  })
  it('says what is wrong, where', () => {
    const r = prepare([{ label: 'a', value: 1, when: '2026-01-01' }, { label: 'b', value: 'abc', when: 'soon' }, { label: 'c', when: '2026-01-02' }], schema, 'rows', undefined)
    const msgs = r.errors.map((e) => e.message)
    expect(msgs).toContain("field 'value' must be a number, got 'abc' at row 2")
    expect(msgs).toContain("field 'when' must be a date, got 'soon' at row 2")
    expect(msgs).toContain("field 'value' is missing at row 3")
  })
})

// a tiny two-series chart to exercise the engine directly
const probe = defineChart<Array<{ label: string; series: string; value: number }>, { note?: string }>({
  id: '__probe',
  name: 'Probe chart',
  category: 'comparison',
  intents: ['compare'],
  summary: 'test',
  input: fields.rows({ label: fields.label(), series: fields.label(), value: fields.num() }),
  options: z.object({ note: z.string().optional() }),
  shape: 'rows',
  draw(ctx) {
    const series = [...new Set(ctx.data.map((d) => d.series))]
    const marks: Mark[] = ctx.data
      .filter((d) => !ctx.hidden.has(d.series))
      .map((d, i) => ({ type: 'rect', key: `${d.series}:${d.label}`, role: 'bar', series: d.series, x: i * 20, y: 10, width: 10, height: d.value, fill: ctx.theme.cat(series.indexOf(d.series)), pattern: 0, datum: d, label: `${d.label} ${d.series}: ${d.value}` }))
    return { marks, note: ctx.options.note, legend: { items: series.map((s, i) => ({ key: s, label: s, color: ctx.theme.cat(i) })) } }
  },
  describe: ({ data, name }) => `${name}, ${data.length} bars`,
  table: (data) => ({ columns: ['label', 'series', 'value'], rows: data.map((d) => [d.label, d.series, d.value]) }),
})
const rows = [{ label: 'a', series: 's1', value: 5 }, { label: 'b', series: 's2', value: 7 }, { label: 'c', series: 's1', value: 3 }]

describe('define: the engine around a chart', () => {
  it('legend toggle hides a series and reports it', () => {
    const el = host()
    const onLegendToggle = vi.fn()
    const h = probe.render(el, rows, { animate: false, onLegendToggle })
    expect(h.markCount()).toBe(3)
    const btn = el.querySelector<HTMLButtonElement>('.oc-legend button[data-key="s1"]')!
    expect(btn.getAttribute('aria-pressed')).toBe('true')
    btn.click()
    expect(h.markCount()).toBe(1)
    expect(onLegendToggle).toHaveBeenCalledWith('s1', false)
    expect(el.querySelector('.oc-legend button[data-key="s1"]')!.getAttribute('aria-pressed')).toBe('false')
    h.destroy()
  })
  it('keyboard: arrows announce, Enter clicks', () => {
    const el = host()
    const onClick = vi.fn()
    const h = probe.render(el, rows, { animate: false, onClick })
    const svg = el.querySelector('svg')!
    svg.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }))
    svg.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }))
    expect(el.querySelector('[aria-live]')!.textContent).toBe('2 of 3: b s2: 7')
    expect(el.querySelector('.oc-focus-ring')!.getAttribute('visibility')).toBe('visible')
    svg.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
    expect(onClick).toHaveBeenCalledWith(rows[1], 1)
    h.destroy()
  })
  it('hover shows a tooltip; click reports the datum', () => {
    const el = host()
    const onClick = vi.fn()
    const h = probe.render(el, rows, { animate: false, onClick })
    const mark = el.querySelector('[data-mark="2"]')!
    mark.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientX: 5, clientY: 5 }))
    const tip = el.querySelector<HTMLElement>('.oc-tooltip')!
    expect(tip.hidden).toBe(false)
    expect(tip.textContent).toBe('c s1: 3')
    mark.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(onClick).toHaveBeenCalledWith(rows[2], 2)
    h.destroy()
  })
  it('theme option, title, note and patterns', () => {
    const el = host()
    const h = probe.render(el, rows, { animate: false, theme: 'high-contrast', title: 'Quarterly', note: '2 values omitted', patterns: true })
    expect(h.root.getAttribute('data-theme')).toBe('high-contrast')
    expect(el.querySelector('.oc-title')!.textContent).toBe('Quarterly')
    expect(h.summary()).toBe('Quarterly. Probe chart, 3 bars')
    expect(el.querySelector('.oc-note')!.textContent).toBe('2 values omitted')
    expect(el.querySelector('[data-mark]')!.getAttribute('style')).toMatch(/fill:url\(#oc-c\d+-p0-/)
    expect(el.querySelectorAll('pattern').length).toBeGreaterThan(0)
    const svgText = h.exportSVG()
    expect(svgText).toMatch(/fill:#256391/) // high-contrast cat-1, resolved
    h.destroy()
  })
  it('empty, error and recovery', () => {
    const el = host()
    const h = probe.render(el, [], { animate: false })
    expect(h.state).toBe('empty')
    expect(el.querySelector('.oc-state')!.getAttribute('aria-label')).toBe('Probe chart: No data to display')
    h.update([{ label: 'a', series: 's', value: 'x' }])
    expect(h.state).toBe('error')
    expect(el.querySelector('.oc-error')!.textContent).toMatch(/field 'value' must be a number, got 'x' at row 1/)
    h.update(rows)
    expect(h.state).toBe('ok')
    expect(el.querySelector('.oc-state')).toBeNull()
    h.destroy()
  })
  it('invalid options are errors, not crashes', () => {
    const el = host()
    const h = probe.render(el, rows, { animate: false, renderer: 'webgl' as 'svg' })
    expect(h.state).toBe('error')
    expect(h.errors[0]!.message).toMatch(/option 'renderer'/)
    h.destroy()
  })
  it('renderer: canvas on request and above the threshold', () => {
    const el = host()
    const h = probe.render(el, rows, { animate: false, canvasThreshold: 2 })
    expect(h.renderer).toBe('canvas')
    expect(el.querySelector('canvas')).not.toBeNull()
    h.update(rows, { canvasThreshold: 5000 })
    expect(h.renderer).toBe('svg')
    expect(el.querySelector('canvas')).toBeNull()
    h.destroy()
  })
  it('exportPNG needs a browser and says so', async () => {
    const el = host()
    const h = probe.render(el, rows, { animate: false })
    await expect(h.exportPNG()).rejects.toThrow(/exportPNG needs/)
    h.destroy()
  })
  it('responds to its container resizing (ResizeObserver)', () => {
    let w = 640
    const el = document.createElement('div')
    document.body.appendChild(el)
    Object.defineProperty(el, 'clientWidth', { get: () => w })
    const raf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 1 }) as typeof requestAnimationFrame
    try {
      const h = probe.render(el, rows, { animate: false })
      expect(el.querySelector('svg')!.getAttribute('width')).toBe('640')
      w = 300
      globalThis.__ro.fire(el, 300, 200)
      expect(el.querySelector('svg')!.getAttribute('width')).toBe('300')
      h.destroy()
    } finally {
      globalThis.requestAnimationFrame = raf
    }
  })
})
