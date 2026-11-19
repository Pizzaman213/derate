// defineChart: a ChartDef (schema + draw + describe + table) becomes a module with
// render() -> ChartHandle. Everything outside the chart's own geometry lives here:
// sizing and ResizeObserver, the DOM skeleton, validation and the empty/error states,
// SVG or canvas rendering, legend toggles, hover highlight, tooltips, keyboard
// navigation with announcements, the hidden table, zoom/brush, exports, and teardown.

import { select } from 'd3-selection'
import { zoom as d3zoom, zoomIdentity, type ZoomBehavior } from 'd3-zoom'
import { brush as d3brush, brushX, type BrushBehavior } from 'd3-brush'
import type { z } from 'zod'
import { cartesian } from './cartesian'
import { el, fillTooltip, placeTooltip, renderLegend, renderState, renderTable } from './dom'
import { exportCSV as toCSV, exportPNG as toPNG, exportSVG as toSVG } from './export'
import { baseOptions } from './fields'
import { makeFormatters } from './format'
import { getModule, register } from './registry'
import { CanvasScene, SvgScene, anchor, boxOf, translateOf, type IndexedMark } from './scene'
import { hashSeed, mulberry32 } from './stats'
import { measure, truncate } from './text'
import { DURATION_MS, effectiveTheme, injectStyles, palette } from './theme'
import { prepare } from './validate'
import type { AnyChartModule, BaseOptions, ChartDef, Layer, ChartHandle, ChartModule, DrawContext, DrawResult, Mark, Rect, ValidationIssue, ZoomState } from './types'

let uidCounter = 0
const DEFAULT_THRESHOLD = 5000
const SVG_NS = 'http://www.w3.org/2000/svg'
/** Painted on the canvas in canvas mode (in this order); labels, axes and the focus ring stay SVG. */
const CANVAS_LAYERS: ReadonlySet<Layer> = new Set<Layer>(['back', 'grid', 'marks'])

function parseOptions<O>(def: ChartDef<unknown, Record<string, unknown>>, options: unknown): { ok: true; value: O & BaseOptions } | { ok: false; errors: ValidationIssue[] } {
  const schema = baseOptions.merge(def.options as z.AnyZodObject)
  const r = schema.safeParse(options ?? {})
  if (r.success) return { ok: true, value: r.data as O & BaseOptions }
  return { ok: false, errors: r.error.issues.map((i) => ({ path: `options.${i.path.join('.')}`, message: `option '${i.path.join('.')}': ${i.message}` })) }
}

function reducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

/** Build a draw context (also used by drawChild). */
function makeContext<D, O extends Record<string, unknown>>(def: ChartDef<D, O>, data: D, options: O & BaseOptions, width: number, height: number, hidden: ReadonlySet<string>, zoomState: ZoomState | null): DrawContext<D, O> {
  const fmt = makeFormatters(options)
  const ctx: DrawContext<D, O> = {
    data, options, width, height, theme: palette, fmt,
    measure: (t, s, w) => measure(t, s, w),
    truncate: (t, w, s, wt) => truncate(t, w, s, wt),
    hidden, zoom: zoomState, reducedMotion: reducedMotion(),
    random: mulberry32(options.seed ?? hashSeed(def.id)),
    cartesian: (spec) => cartesian({ width, height, fmt, theme: palette, measure: (t, s) => measure(t, s), truncate: (t, w, s) => truncate(t, w, s), zoom: zoomState, zoomAxes: def.zoom, xDomain: options.xDomain, yDomain: options.yDomain }, spec),
    drawChild: (id, childData, childOptions, rect, keyPrefix) => drawChild(id, childData, { ...childOptions, locale: options.locale, timeZone: options.timeZone, currency: options.currency }, rect, keyPrefix),
  }
  return ctx
}

function drawChild(id: string, data: unknown, options: Record<string, unknown>, rect: Rect, keyPrefix: string): DrawResult {
  const mod = getModule(id)
  const shift = `translate(${rect.x},${rect.y})`
  const fail = (msg: string): DrawResult => ({ marks: [{ type: 'text', key: `${keyPrefix}:err`, x: rect.width / 2, y: rect.height / 2, text: msg, anchor: 'middle', baseline: 'central', fill: palette.muted, transform: shift }] })
  if (!mod) return fail(`unknown chart '${id}'`)
  const v = mod.validate(data, options)
  if (!v.ok) return fail(v.errors[0]?.message ?? 'invalid data')
  const opts = parseOptions<Record<string, unknown>>(mod.def, options)
  if (!opts.ok) return fail(opts.errors[0]?.message ?? 'invalid options')
  const empty = (mod.def.isEmpty ?? defaultEmpty)(v.data, opts.value)
  if (empty) return fail(typeof empty === 'string' ? empty : 'No data')
  const res = mod.def.draw(makeContext(mod.def, v.data, opts.value, rect.width, rect.height, new Set(), null))
  return {
    ...res,
    marks: res.marks.map((m) => ({ ...m, key: `${keyPrefix}:${m.key ?? m.role ?? m.type}`, transform: m.transform ? `${shift} ${m.transform}` : shift })),
    plot: res.plot ? { ...res.plot, x: res.plot.x + rect.x, y: res.plot.y + rect.y } : undefined,
  }
}

const defaultEmpty = (d: unknown) => (Array.isArray(d) ? d.length === 0 : d === null || d === undefined)

/** Turn a chart definition into a renderable, registered module. */
export function defineChart<D, O extends Record<string, unknown>>(def: ChartDef<D, O>): ChartModule<D, O> {
  const anyDef = def as unknown as ChartDef<unknown, Record<string, unknown>>
  const validate = (data: unknown, options?: Partial<O & BaseOptions>) => {
    const po = parseOptions<O>(anyDef, options ?? {})
    if (!po.ok) return { ok: false, data: undefined, errors: po.errors }
    const p = prepare<D>(data, def.input, def.shape, options?.fields as Record<string, string> | undefined, def.adapt ? (raw) => def.adapt!(raw, po.value) : undefined, po.value.timeZone)
    return { ok: p.ok, data: p.data, errors: p.errors }
  }

  const render = (container: HTMLElement, data: unknown, options: Partial<O & BaseOptions> = {}): ChartHandle => {
    const doc = container.ownerDocument
    injectStyles(doc)
    const uid = `c${++uidCounter}`
    let rawData = data
    let rawOptions: Record<string, unknown> = { ...options }
    let opts = {} as O & BaseOptions
    let state: 'ok' | 'empty' | 'error' = 'ok'
    let errors: ValidationIssue[] = []
    let canonical: D | undefined
    const hidden = new Set<string>()
    let zoomState: ZoomState | null = null
    let active = -1
    let items: IndexedMark[] = []
    let dataIdx: number[] = []
    let renderer: 'svg' | 'canvas' = 'svg'
    let summary = ''
    let lastResult: DrawResult | null = null
    let destroyed = false
    let firstPaint = true
    let raf = 0

    const heightFromContainer = container.clientHeight > 0
    // DOM skeleton
    const root = el(doc, 'div', 'oc-root')
    root.setAttribute('data-chart', def.id)
    const titleEl = el(doc, 'div', 'oc-title')
    const legendEl = el(doc, 'ul', 'oc-legend')
    legendEl.setAttribute('aria-label', 'Legend')
    const stage = el(doc, 'div', 'oc-stage')
    const scene = new SvgScene(doc, { animate: false, duration: DURATION_MS, patterns: false, theme: 'light', uid })
    const svg = scene.svg
    svg.setAttribute('role', 'img')
    svg.setAttribute('tabindex', '0')
    let canvas: CanvasScene | null = null
    const ring = doc.createElementNS(SVG_NS, 'rect')
    ring.setAttribute('class', 'oc-focus-ring')
    ring.setAttribute('rx', '2')
    ring.setAttribute('visibility', 'hidden')
    const brushG = doc.createElementNS(SVG_NS, 'g')
    brushG.setAttribute('class', 'oc-brush')
    stage.appendChild(svg)
    const noteEl = el(doc, 'div', 'oc-note')
    const live = el(doc, 'div', 'oc-sr')
    live.setAttribute('aria-live', 'polite')
    live.setAttribute('aria-atomic', 'true')
    const tip = el(doc, 'div', 'oc-tooltip')
    tip.hidden = true
    tip.setAttribute('aria-hidden', 'true')
    const table = el(doc, 'table')
    const tableWrap = el(doc, 'div', 'oc-table-wrap oc-sr')
    tableWrap.appendChild(table)
    const stateEl = el(doc, 'div', 'oc-state')
    root.append(titleEl, legendEl, stage, noteEl, live, tableWrap, tip)
    container.appendChild(root)

    // listeners, all tracked so destroy() leaves none behind
    const offs: Array<() => void> = []
    const on = <E extends Event>(target: EventTarget, type: string, fn: (e: E) => void) => {
      target.addEventListener(type, fn as EventListener)
      offs.push(() => target.removeEventListener(type, fn as EventListener))
    }

    const size = () => {
      const w = Math.max(80, Math.floor(opts.width ?? (container.clientWidth || 640)))
      const aspect = def.aspect ?? 0.62
      const auto = Math.round(Math.min(def.maxHeight ?? 560, Math.max(def.minHeight ?? 160, w * aspect)))
      const h = Math.max(Math.min(60, def.minHeight ?? 60), Math.floor(opts.height ?? (heightFromContainer ? container.clientHeight || auto : auto)))
      return { w, h }
    }

    const theme = () => effectiveTheme(root)

    const showState = (kind: 'empty' | 'error', h: number, message?: string) => {
      state = kind
      items = []
      dataIdx = []
      lastResult = null
      stage.hidden = true
      legendEl.hidden = true
      noteEl.hidden = true
      table.replaceChildren()
      if (!stateEl.parentNode) root.insertBefore(stateEl, stage)
      stateEl.hidden = false
      stateEl.style.height = `${Math.max(96, h - (titleEl.hidden ? 0 : titleEl.offsetHeight + 4))}px`
      const name = opts.title ?? def.name
      const msg = opts.emptyMessage ?? message ?? 'No data to display'
      summary = kind === 'empty' ? `${name}: ${msg}` : `${name}: can't draw this data`
      renderState(doc, stateEl, kind, name, msg, errors)
    }

    const draw = (animate: boolean) => {
      if (destroyed) return
      const { w, h } = size()
      root.style.height = heightFromContainer || opts.height ? `${h}px` : ''
      const th = opts.theme && opts.theme !== 'auto' ? opts.theme : null
      if (th) root.setAttribute('data-theme', th)
      else root.removeAttribute('data-theme')
      titleEl.textContent = opts.title ?? ''
      titleEl.hidden = !opts.title
      if (state === 'error' || canonical === undefined) return showState('error', h)
      const empty = (def.isEmpty ?? defaultEmpty)(canonical, opts)
      if (empty) return showState('empty', h, typeof empty === 'string' ? empty : undefined)
      state = 'ok'
      stateEl.remove()
      stage.hidden = false
      const run = (svgH: number) => {
        const floor = Math.min(40, def.minHeight ?? 40)
        const ctx = makeContext(def, canonical as D, opts, w, Math.max(floor, svgH), hidden, zoomState)
        return { res: def.draw(ctx), svgH: Math.max(floor, svgH) }
      }
      const titleH = titleEl.hidden ? 0 : titleEl.offsetHeight + 4
      let { res, svgH } = run(h - titleH)
      const showLegend = opts.legend !== false && !!res.legend
      renderLegend(doc, legendEl, showLegend ? res.legend : undefined, hidden)
      noteEl.textContent = res.note ?? ''
      noteEl.hidden = !res.note
      const chrome = titleH + (legendEl.hidden ? 0 : legendEl.offsetHeight + 6) + (noteEl.hidden ? 0 : noteEl.offsetHeight + 4)
      if (chrome > titleH) {
        ;({ res, svgH } = run(h - chrome))
        // the second pass may say something different (e.g. "N more not shown")
        renderLegend(doc, legendEl, showLegend && res.legend ? res.legend : undefined, hidden)
        noteEl.textContent = res.note ?? ''
        noteEl.hidden = !res.note
      }
      lastResult = res
      const marks: Mark[] = res.marks.filter((m) => m.series === undefined || !hidden.has(m.series) || m.datum === undefined)
      items = marks.map((mark, index) => ({ mark, index }))
      dataIdx = items.filter((it) => it.mark.datum !== undefined).map((it) => it.index)
      const n = dataIdx.length
      renderer = opts.renderer === 'canvas' || (opts.renderer !== 'svg' && n > (opts.canvasThreshold ?? DEFAULT_THRESHOLD)) ? 'canvas' : 'svg'
      const t = theme()
      const doAnimate = animate && opts.animate !== false && !reducedMotion() && renderer === 'svg' && typeof requestAnimationFrame !== 'undefined'
      scene.setOptions({ animate: doAnimate, patterns: !!opts.patterns, theme: t })
      scene.size(w, svgH)
      scene.setClip(res.clip)
      if (renderer === 'canvas') {
        if (!canvas) {
          canvas = new CanvasScene(doc)
          stage.insertBefore(canvas.canvas, svg)
          svg.style.position = 'relative'
        }
        canvas.draw(items.filter((it) => CANVAS_LAYERS.has(it.mark.layer ?? 'marks')), w, svgH, t, res.clip)
        scene.render(items, CANVAS_LAYERS)
      } else {
        if (canvas) { canvas.destroy(); canvas = null; svg.style.position = '' }
        scene.render(items)
      }
      if (brushG.parentNode !== svg) svg.appendChild(brushG)
      if (ring.parentNode !== svg || svg.lastChild !== ring) svg.appendChild(ring)
      // a11y
      const name = opts.title ? `${opts.title}. ` : ''
      summary = `${name}${def.describe({ data: canonical as D, options: opts, fmt: makeFormatters(opts), name: def.name })}${opts.description ? ` ${opts.description}` : ''}`
      svg.setAttribute('aria-label', summary)
      svg.setAttribute('aria-describedby', `oc-${uid}-table`)
      renderTable(doc, table, `oc-${uid}-table`, summary, def.table(canonical as D, opts, makeFormatters(opts)), !!opts.showTable)
      if (active >= n) active = -1
      if (active >= 0) showFocus(active, false)
      else ring.setAttribute('visibility', 'hidden')
      setupInteractions(res)
      firstPaint = false
    }

    // ----- hover, tooltip, focus
    const markAt = (i: number) => items[dataIdx[i] ?? -1]?.mark
    const screenPoint = (m: Mark): [number, number] => {
      const r = svg.getBoundingClientRect()
      const [ax, ay] = anchor(m)
      const [tx, ty] = translateOf(m.transform)
      return [r.left + ax + tx, r.top + ay + ty]
    }
    const tooltipFor = (m: Mark) => m.tooltip ?? m.label ?? ''
    /** Dim every mark not in `key` (a series, or a mark's hoverKey). */
    const highlight = (key: string | null, by: 'series' | 'hover' = 'series') => {
      if (renderer === 'canvas' && canvas && lastResult) canvas.draw(items.filter((it) => CANVAS_LAYERS.has(it.mark.layer ?? 'marks')), size().w, Number(svg.getAttribute('height')), theme(), lastResult.clip, key, by)
      else scene.dim(key, by)
    }
    const highlightMark = (m: Mark) => (m.hoverKey !== undefined ? highlight(m.hoverKey, 'hover') : highlight(m.series ?? null))
    const showFocus = (i: number, announce: boolean) => {
      const m = markAt(i)
      if (!m) return
      active = i
      const b = boxOf(m)
      const [tx, ty] = translateOf(m.transform)
      ring.setAttribute('x', String(b.x + tx - 2))
      ring.setAttribute('y', String(b.y + ty - 2))
      ring.setAttribute('width', String(b.width + 4))
      ring.setAttribute('height', String(b.height + 4))
      ring.setAttribute('visibility', 'visible')
      if (announce) {
        live.textContent = `${i + 1} of ${dataIdx.length}: ${m.label ?? ''}`
        if (opts.tooltip !== false) {
          fillTooltip(doc, tip, tooltipFor(m))
          const [x, y] = screenPoint(m)
          placeTooltip(tip, x, y)
        }
        highlightMark(m)
        opts.onHover?.(m.datum, i)
      }
    }
    const clearFocus = () => {
      ring.setAttribute('visibility', 'hidden')
      tip.hidden = true
      highlight(null)
    }
    const indexFromEvent = (e: PointerEvent | MouseEvent): number => {
      if (renderer === 'canvas' && canvas) {
        const r = svg.getBoundingClientRect()
        const hit = canvas.hit(e.clientX - r.left, e.clientY - r.top)
        if (hit) return dataIdx.indexOf(hit.index)
        return nearestX(e)
      }
      const t = (e.target as Element | null)?.closest?.('[data-mark]')
      if (!t || !svg.contains(t)) return (e.target as Element | null)?.closest?.('[data-full]') ? -1 : nearestX(e)
      return dataIdx.indexOf(Number(t.getAttribute('data-mark')))
    }
    /** `hover: 'nearest-x'`: over the plot, the data mark closest in x (ties: closest in y). */
    const nearestX = (e: PointerEvent | MouseEvent): number => {
      const p = lastResult?.plot
      if (def.hover !== 'nearest-x' || !p) return -1
      const r = svg.getBoundingClientRect()
      const px = e.clientX - r.left, py = e.clientY - r.top
      if (px < p.x - 8 || px > p.x + p.width + 8 || py < p.y - 8 || py > p.y + p.height + 8) return -1
      let best = -1, bx = Infinity, by = Infinity
      for (let i = 0; i < dataIdx.length; i++) {
        const m = items[dataIdx[i]!]!.mark
        const [ax, ay] = anchor(m)
        const [tx, ty] = translateOf(m.transform)
        const dx = Math.abs(ax + tx - px), dy = Math.abs(ay + ty - py)
        if (dx < bx - 0.5 || (dx <= bx + 0.5 && dy < by)) { best = i; bx = dx; by = dy }
      }
      return best
    }
    /** Ring the hovered mark when it may be invisible (hidden dots); keyboard focus keeps the ring. */
    const hoverRing = (m: Mark | null) => {
      if (def.hover !== 'nearest-x' || active >= 0) return
      if (!m) return ring.setAttribute('visibility', 'hidden')
      const b = boxOf(m)
      const [tx, ty] = translateOf(m.transform)
      ring.setAttribute('x', String(b.x + tx - 2))
      ring.setAttribute('y', String(b.y + ty - 2))
      ring.setAttribute('width', String(b.width + 4))
      ring.setAttribute('height', String(b.height + 4))
      ring.setAttribute('visibility', 'visible')
    }
    on<PointerEvent>(svg, 'pointermove', (e) => {
      const i = indexFromEvent(e)
      if (i >= 0) {
        const m = markAt(i)!
        if (opts.tooltip !== false) {
          fillTooltip(doc, tip, tooltipFor(m))
          placeTooltip(tip, e.clientX, e.clientY)
        }
        highlightMark(m)
        hoverRing(m)
        opts.onHover?.(m.datum, i)
        return
      }
      const full = (e.target as Element | null)?.closest?.('[data-full]')
      if (full) {
        fillTooltip(doc, tip, full.getAttribute('data-full') ?? '')
        placeTooltip(tip, e.clientX, e.clientY)
        return
      }
      tip.hidden = true
      highlight(null)
      hoverRing(null)
    })
    on(svg, 'pointerleave', () => {
      tip.hidden = true
      highlight(null)
      hoverRing(null)
      opts.onHover?.(null, -1)
    })
    on<MouseEvent>(svg, 'click', (e) => {
      const i = indexFromEvent(e)
      if (i >= 0) opts.onClick?.(markAt(i)!.datum, i)
    })
    on<KeyboardEvent>(svg, 'keydown', (e) => {
      const n = dataIdx.length
      if (!n) return
      let next = active
      switch (e.key) {
        case 'ArrowRight': case 'ArrowDown': next = active < 0 ? 0 : Math.min(n - 1, active + 1); break
        case 'ArrowLeft': case 'ArrowUp': next = active < 0 ? 0 : Math.max(0, active - 1); break
        case 'Home': next = 0; break
        case 'End': next = n - 1; break
        case 'PageDown': next = Math.min(n - 1, Math.max(0, active) + Math.max(1, Math.round(n / 10))); break
        case 'PageUp': next = Math.max(0, active - Math.max(1, Math.round(n / 10))); break
        case 'Enter': case ' ':
          if (active >= 0) { opts.onClick?.(markAt(active)!.datum, active); e.preventDefault() }
          return
        case 'Escape': active = -1; clearFocus(); live.textContent = ''; return
        default: return
      }
      e.preventDefault()
      showFocus(next, true)
    })
    on(svg, 'blur', () => clearFocus())
    on<MouseEvent>(legendEl, 'click', (e) => {
      const b = (e.target as Element | null)?.closest?.('button[data-key]')
      if (!b) return
      const key = b.getAttribute('data-key')!
      const visible = hidden.has(key)
      if (visible) hidden.delete(key)
      else hidden.add(key)
      opts.onLegendToggle?.(key, visible)
      draw(true)
      for (const btn of legendEl.querySelectorAll<HTMLElement>('button[data-key]')) if (btn.getAttribute('data-key') === key) btn.focus()
    })
    on<MouseEvent>(legendEl, 'pointerover', (e) => {
      const b = (e.target as Element | null)?.closest?.('[data-key]')
      highlight(b ? b.getAttribute('data-key') : null)
    })
    on(legendEl, 'pointerleave', () => highlight(null))

    // ----- zoom & brush
    let zoomer: ZoomBehavior<SVGSVGElement, unknown> | null = null
    let brusher: BrushBehavior<unknown> | null = null
    const setupInteractions = (res: DrawResult) => {
      const wantZoom = !!def.zoom && !!opts.zoom
      const wantBrush = !!def.zoom && !!opts.brush && !!res.plot
      if (wantZoom && !zoomer) {
        zoomer = d3zoom<SVGSVGElement, unknown>().scaleExtent([1, 64]).on('zoom', (ev) => {
          zoomState = { k: ev.transform.k, x: ev.transform.x, y: ev.transform.y }
          opts.onZoom?.(zoomState)
          draw(false)
        })
        if (wantBrush) zoomer.filter((ev: Event) => ev.type === 'wheel')
        select(svg).call(zoomer).on('dblclick.zoom', null)
        on(svg, 'dblclick', () => { if (zoomer) select(svg).call(zoomer.transform, zoomIdentity) })
      } else if (!wantZoom && zoomer) {
        select(svg).on('.zoom', null)
        zoomer = null
        zoomState = null
      }
      if (zoomer && res.plot) zoomer.extent([[res.plot.x, res.plot.y], [res.plot.x + res.plot.width, res.plot.y + res.plot.height]]).translateExtent([[res.plot.x, res.plot.y], [res.plot.x + res.plot.width, res.plot.y + res.plot.height]])
      if (wantBrush && res.plot) {
        const p = res.plot
        const ext: [[number, number], [number, number]] = [[p.x, p.y], [p.x + p.width, p.y + p.height]]
        if (!brusher) {
          brusher = (def.zoom === 'xy' ? d3brush() : brushX()).on('end', (ev) => {
            const s = ev.selection as [number, number] | [[number, number], [number, number]] | null
            if (!s) return opts.onBrush?.(null)
            if (typeof s[0] === 'number') {
              const [a, b] = s as [number, number]
              opts.onBrush?.([lastResult?.invertX?.(a), lastResult?.invertX?.(b)])
            } else {
              const [[x0], [x1]] = s as [[number, number], [number, number]]
              opts.onBrush?.([lastResult?.invertX?.(x0), lastResult?.invertX?.(x1)])
            }
          })
        }
        brusher.extent(ext)
        select(brushG).call(brusher)
      } else if (brusher) {
        select(brushG).on('.brush', null).selectAll('*').remove()
        brusher = null
      }
    }

    // ----- sizing
    const schedule = () => {
      if (destroyed) return
      if (typeof requestAnimationFrame === 'undefined') return draw(false)
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => draw(false))
    }
    let lastW = -1, lastH = -1
    const ro = typeof ResizeObserver !== 'undefined'
      ? new ResizeObserver((entries) => {
          const r = entries[0]?.contentRect
          if (!r) return
          if (Math.round(r.width) === lastW && (!heightFromContainer || Math.round(r.height) === lastH)) return
          lastW = Math.round(r.width)
          lastH = Math.round(r.height)
          if (opts.width === undefined || (heightFromContainer && opts.height === undefined)) schedule()
        })
      : null
    ro?.observe(container)

    // ----- data
    const ingest = () => {
      const po = parseOptions<O>(anyDef, rawOptions)
      if (!po.ok) {
        opts = rawOptions as O & BaseOptions
        state = 'error'
        errors = po.errors
        canonical = undefined
        return
      }
      opts = po.value
      const v = validate(rawData, opts)
      errors = v.errors
      canonical = v.ok ? v.data : undefined
      state = v.ok ? 'ok' : 'error'
    }
    ingest()
    draw(true)
    lastW = size().w
    // laid out with a fallback font? lay out again once the web fonts are in
    const fonts = doc.fonts
    if (fonts && fonts.status !== 'loaded' && typeof fonts.ready?.then === 'function') fonts.ready.then(() => { if (!destroyed) draw(false) })

    const handle: ChartHandle = {
      type: def.id,
      root,
      get state() { return state },
      get errors() { return errors },
      get renderer() { return renderer },
      update(newData, newOptions) {
        if (destroyed) return
        rawData = newData
        if (newOptions) rawOptions = { ...rawOptions, ...newOptions }
        ingest()
        draw(!firstPaint)
      },
      resize(w, h) {
        if (destroyed) return
        if (w !== undefined) rawOptions.width = opts.width = w
        if (h !== undefined) rawOptions.height = opts.height = h
        draw(false)
      },
      destroy() {
        if (destroyed) return
        destroyed = true
        if (typeof cancelAnimationFrame !== 'undefined') cancelAnimationFrame(raf)
        ro?.disconnect()
        if (zoomer) select(svg).on('.zoom', null)
        if (brusher) select(brushG).on('.brush', null)
        for (const off of offs) off()
        offs.length = 0
        canvas?.destroy()
        scene.destroy()
        root.remove()
      },
      exportSVG() {
        if (state !== 'ok') return ''
        return toSVG(svg, { theme: theme(), title: summary, canvas: canvas?.canvas })
      },
      exportPNG(scale = 2) {
        if (state !== 'ok') return Promise.reject(new Error(`nothing to export: the chart is in its ${state} state`))
        return toPNG(handle.exportSVG(), Number(svg.getAttribute('width')), Number(svg.getAttribute('height')), scale)
      },
      exportCSV() {
        if (canonical === undefined) return ''
        return toCSV(def.table(canonical, opts, makeFormatters(opts)))
      },
      markCount: () => dataIdx.length,
      summary: () => summary,
      focusMark(i) {
        svg.focus()
        showFocus(Math.max(0, Math.min(dataIdx.length - 1, i)), true)
      },
    }
    return handle
  }

  const mod: ChartModule<D, O> = { def, render, validate }
  register(mod as unknown as AnyChartModule)
  return mod
}
