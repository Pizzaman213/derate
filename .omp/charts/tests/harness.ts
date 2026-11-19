// The suite every chart runs: every sample validates and renders, mark counts, the
// aria summary, keyboard announcements, table/CSV agreement, a standalone SVG export,
// update with and without transitions, resize, and a destroy that leaves no DOM, no
// listeners and no ResizeObserver behind. Chart tests add their own math on top.

import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { AnyChartModule, ChartHandle } from '../core'

export interface Sample {
  name: string
  description?: string
  data: unknown
  options?: Record<string, unknown>
}

/** Every sample in a chart folder, basic and realistic first. */
export function loadSamples(chartDir: string): Sample[] {
  const dir = join(chartDir, 'samples')
  const files = readdirSync(dir).filter((f) => f.endsWith('.json'))
  const rank = (f: string) => (f === 'basic.json' ? 0 : f === 'realistic.json' ? 1 : 2)
  return files
    .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
    .map((f) => ({ name: f.replace(/\.json$/, ''), ...(JSON.parse(readFileSync(join(dir, f), 'utf8')) as Omit<Sample, 'name'>) }))
}

type Listeners = Map<EventTarget, Map<string, Set<unknown>>>
/** Count listeners added and not removed while it is installed. */
export function trackListeners() {
  const live: Listeners = new Map()
  const proto = EventTarget.prototype
  const add = proto.addEventListener, remove = proto.removeEventListener
  proto.addEventListener = function (this: EventTarget, type: string, fn: EventListenerOrEventListenerObject | null, o?: boolean | AddEventListenerOptions) {
    if (fn) {
      let byType = live.get(this)
      if (!byType) live.set(this, (byType = new Map()))
      let set = byType.get(type)
      if (!set) byType.set(type, (set = new Set()))
      set.add(fn)
    }
    return add.call(this, type, fn, o)
  }
  proto.removeEventListener = function (this: EventTarget, type: string, fn: EventListenerOrEventListenerObject | null, o?: boolean | EventListenerOptions) {
    if (fn) live.get(this)?.get(type)?.delete(fn)
    return remove.call(this, type, fn, o)
  }
  return {
    count: () => [...live.values()].reduce((n, m) => n + [...m.values()].reduce((k, s) => k + s.size, 0), 0),
    restore: () => {
      proto.addEventListener = add
      proto.removeEventListener = remove
    },
  }
}

export function host(width = 640, height = 400): HTMLDivElement {
  const div = document.createElement('div')
  document.body.appendChild(div)
  Object.defineProperty(div, 'clientWidth', { configurable: true, get: () => width })
  Object.defineProperty(div, 'clientHeight', { configurable: true, get: () => (height ? height : div.scrollHeight) })
  return div
}

export function renderSample(mod: AnyChartModule, s: Sample, extra: Record<string, unknown> = {}, w = 640, h = 400): { el: HTMLDivElement; h: ChartHandle } {
  const el = host(w, h)
  const handle = mod.render(el, s.data, { animate: false, ...s.options, ...extra })
  return { el, h: handle }
}

const roleCount = (el: Element, role: string) => el.querySelectorAll(`[data-role="${role}"][data-mark]`).length
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))

export interface SuiteSpec {
  module: AnyChartModule
  /** The chart folder (import.meta.dirname of the test). */
  dir: string
  /** Expected data-mark counts per sample name (svg path: counts [data-role=role][data-mark]; canvas: markCount()). */
  marks?: Record<string, number>
  /** The role counted by `marks` (default: every data mark). */
  role?: string
  /** The summary of basic.json must match. */
  summary?: RegExp
  /** Samples that must land in the empty state (default: names containing "empty"). */
  empty?: string[]
  /** Inputs that must fail validation, with the message expected. */
  malformed: Array<{ name: string; data: unknown; options?: Record<string, unknown>; message: RegExp }>
}

export function chartSuite(spec: SuiteSpec): void {
  const { module: mod } = spec
  const samples = loadSamples(spec.dir)
  const id = mod.def.id
  const isEmptySample = (n: string) => (spec.empty ? spec.empty.includes(n) : n.includes('empty'))

  describe(`${id}: samples`, () => {
    it('has basic, realistic and edge samples', () => {
      const names = samples.map((s) => s.name)
      expect(names).toContain('basic')
      expect(names).toContain('realistic')
      expect(names.filter((n) => n.startsWith('edge-')).length).toBeGreaterThanOrEqual(3)
    })
    for (const s of samples) {
      describe(s.name, () => {
        it('validates against the schema', () => {
          const v = mod.validate(s.data, s.options)
          expect(v.errors).toEqual([])
          expect(v.ok).toBe(true)
        })
        it('renders without throwing, with a summary, table and export', () => {
          const { el, h } = renderSample(mod, s)
          try {
            expect(h.errors).toEqual([])
            if (isEmptySample(s.name)) {
              expect(h.state).toBe('empty')
              const plate = el.querySelector('.oc-state')!
              expect(plate.getAttribute('role')).toBe('img')
              expect(plate.textContent!.length).toBeGreaterThan(0)
              return
            }
            expect(h.state).toBe('ok')
            const svg = el.querySelector('svg.oc-svg')!
            expect(svg.getAttribute('role')).toBe('img')
            const label = svg.getAttribute('aria-label') ?? ''
            expect(label.length).toBeGreaterThan(10)
            expect(label).not.toMatch(/undefined|NaN|\[object/)
            if (s.name === 'basic' && spec.summary) expect(label).toMatch(spec.summary)
            const n = h.markCount()
            expect(n).toBeGreaterThan(0)
            if (h.renderer === 'svg') expect(svg.querySelectorAll('[data-mark]').length).toBe(n)
            const want = spec.marks?.[s.name]
            if (want !== undefined) {
              if (h.renderer === 'svg' && spec.role) expect(roleCount(svg, spec.role)).toBe(want)
              else expect(n).toBe(want)
            }
            // no NaN geometry anywhere
            expect(svg.innerHTML).not.toMatch(/NaN|undefined|Infinity/)
            // table <-> CSV agreement
            const csv = h.exportCSV()
            const tableRows = el.querySelectorAll('table tbody tr').length
            const csvRows = csv.trim() ? csv.trim().split(/\r?\n(?=(?:[^"]*"[^"]*")*[^"]*$)/).length - 1 : 0
            expect(Math.min(csvRows, 501)).toBe(Math.min(tableRows, 501))
            // standalone SVG export: literal colours only
            const out = h.exportSVG()
            expect(out.startsWith('<svg')).toBe(true)
            expect(out).not.toMatch(/var\(--/)
            expect(new DOMParser().parseFromString(out, 'image/svg+xml').querySelector('parsererror')).toBeNull()
            // keyboard: first arrow announces mark 1 of n
            svg.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
            expect(el.querySelector('[aria-live]')!.textContent).toMatch(new RegExp(`^1 of ${n}: .+`))
            svg.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))
            expect(el.querySelector('[aria-live]')!.textContent).toMatch(new RegExp(`^${n} of ${n}: .+`))
          } finally {
            h.destroy()
            el.remove()
          }
        })
      })
    }
  })

  describe(`${id}: malformed input`, () => {
    for (const m of spec.malformed) {
      it(`rejects ${m.name}`, () => {
        const v = mod.validate(m.data, m.options)
        expect(v.ok).toBe(false)
        expect(v.errors.map((e) => e.message).join('\n')).toMatch(m.message)
        const { el, h } = renderSample(mod, { name: m.name, data: m.data, options: m.options })
        try {
          expect(h.state).toBe('error')
          const plate = el.querySelector('.oc-state.oc-error')!
          expect(plate.getAttribute('role')).toBe('alert')
          expect(plate.textContent).toMatch(m.message)
        } finally {
          h.destroy()
          el.remove()
        }
      })
    }
  })

  describe(`${id}: lifecycle`, () => {
    const basic = samples.find((s) => s.name === 'basic')!
    const realistic = samples.find((s) => s.name === 'realistic')!
    it('update() swaps data and options', () => {
      const { el, h } = renderSample(mod, basic)
      try {
        const before = h.markCount()
        expect(before).toBeGreaterThan(0)
        h.update(realistic.data, { ...realistic.options })
        expect(h.state).toBe('ok')
        const fresh = renderSample(mod, realistic)
        expect(h.markCount()).toBe(fresh.h.markCount())
        fresh.h.destroy()
        fresh.el.remove()
        // options merge on update: clear realistic's keys explicitly to go back to basic
        const reset = Object.fromEntries(Object.keys(realistic.options ?? {}).map((k) => [k, undefined]))
        h.update([], reset)
        expect(['empty', 'error', 'ok']).toContain(h.state)
        h.update(basic.data, { ...reset, ...basic.options })
        expect(h.markCount()).toBe(before)
      } finally {
        h.destroy()
        el.remove()
      }
    })
    it('update() transitions: kept marks reuse their nodes, exits are removed', async () => {
      const { el, h } = renderSample(mod, basic, { animate: true })
      try {
        const svg = el.querySelector('svg.oc-svg')!
        const first = svg.querySelector('[data-mark]')
        h.update(basic.data, { ...basic.options, animate: true })
        expect(svg.querySelector('[data-mark]')).toBe(first)
        h.update(realistic.data, { ...realistic.options, animate: true })
        await wait(900)
        expect(svg.querySelectorAll('[data-exiting]').length).toBe(0)
        if (h.renderer === 'svg') expect(svg.querySelectorAll('[data-mark]').length).toBe(h.markCount())
      } finally {
        h.destroy()
        el.remove()
      }
    })
    it('resize() reflows to the new width', () => {
      const { el, h } = renderSample(mod, basic)
      try {
        h.resize(320, 240)
        const svg = el.querySelector('svg.oc-svg')!
        expect(svg.getAttribute('width')).toBe('320')
        expect(Number(svg.getAttribute('height'))).toBeLessThanOrEqual(240)
        expect(h.markCount()).toBeGreaterThan(0)
        expect(svg.innerHTML).not.toMatch(/NaN/)
        // the ResizeObserver path too
        h.resize(undefined, undefined)
      } finally {
        h.destroy()
        el.remove()
      }
    })
    it('destroy() leaves no DOM, listeners or observers', () => {
      const ro = globalThis.__ro.live.size
      const t = trackListeners()
      try {
        const el = host()
        const h = mod.render(el, basic.data, { ...basic.options, animate: false, zoom: true })
        expect(t.count()).toBeGreaterThan(0)
        h.destroy()
        expect(el.childNodes.length).toBe(0)
        expect(t.count()).toBe(0)
        expect(globalThis.__ro.live.size).toBe(ro)
        h.update(basic.data)
        expect(el.childNodes.length).toBe(0)
        el.remove()
      } finally {
        t.restore()
      }
    })
  })
}
