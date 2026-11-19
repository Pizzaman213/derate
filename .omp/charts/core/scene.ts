// Marks -> pixels. SVG through a keyed d3 join (enter fades in, updates interpolate,
// exits fade out), or -- above the canvas threshold -- the marks layer painted to a
// HiDPI canvas while axes, labels and the focus ring stay in SVG.

import { select, type Selection } from 'd3-selection'
import 'd3-transition'
import { quadtree, type Quadtree } from 'd3-quadtree'
import { LAYERS, type Layer, type Mark, type Rect } from './types'
import { PATTERNS, resolvePaint, ease, FONT, FONT_MONO, type ThemeName } from './theme'
import { measure } from './text'

const SVG_NS = 'http://www.w3.org/2000/svg'
export interface SceneOptions {
  animate: boolean
  duration: number
  patterns: boolean
  theme: ThemeName
  uid: string
}
export interface IndexedMark {
  mark: Mark
  index: number
}

/** Key used for the join: type-qualified so an element never changes tag. */
export function markKey(m: Mark, i: number): string {
  // control characters are not allowed in XML attributes (exported SVG must parse)
  return `${m.type}:${m.key ?? `${m.role ?? m.layer ?? 'm'}#${i}`}`.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, '\u2400')
}

const TAG: Record<Mark['type'], string> = { rect: 'rect', circle: 'circle', path: 'path', line: 'line', text: 'text' }

function geometry(m: Mark): Record<string, string | number | undefined> {
  switch (m.type) {
    case 'rect': {
      // a data mark is never thinner than 1px: sub-pixel bars can be neither seen nor hovered
      const min = m.datum !== undefined ? 1 : 0
      // zero-height (or zero-width) data rects still get a 1px hit line when the other side is real
      const w = m.width > 0 || (min && m.height > 0) ? Math.max(min, m.width) : 0
      const h = m.height > 0 || (min && m.width > 0) ? Math.max(min, m.height) : 0
      return { x: r(m.x), y: r(m.y - (h - Math.max(0, m.height)) / 2), width: r(w), height: r(h), rx: m.rx }
    }
    case 'circle': return { cx: r(m.cx), cy: r(m.cy), r: r(Math.max(0, m.r)) }
    case 'path': return { d: m.d.includes('.') ? m.d.replace(LONG_DECIMAL, (x) => String(Math.round(+x * 100) / 100)) : m.d }
    case 'line': return { x1: r(m.x1), y1: r(m.y1), x2: r(m.x2), y2: r(m.y2) }
    case 'text': return { x: r(m.x), y: r(m.y), transform: m.rotate ? `rotate(${m.rotate} ${r(m.x)} ${r(m.y)})` : undefined }
  }
}
/** Path coordinates rounded like every other attribute (0.01 px), so paths and circles agree. */
const LONG_DECIMAL = /-?\d+\.\d{3,}(?:e-?\d+)?/g
const r = (n: number) => (Number.isFinite(n) ? Math.round(n * 100) / 100 : 0)

function patternId(uid: string, idx: number, fill: string) {
  let h = 0
  for (let i = 0; i < fill.length; i++) h = (h * 31 + fill.charCodeAt(i)) | 0
  return `oc-${uid}-p${idx % PATTERNS.length}-${(h >>> 0).toString(36)}`
}

function ensurePattern(defs: SVGDefsElement, id: string, idx: number, fill: string) {
  if (defs.querySelector(`#${id}`)) return
  const p = PATTERNS[idx % PATTERNS.length]!
  const doc = defs.ownerDocument
  const pat = doc.createElementNS(SVG_NS, 'pattern')
  pat.setAttribute('id', id)
  pat.setAttribute('patternUnits', 'userSpaceOnUse')
  pat.setAttribute('width', String(p.size))
  pat.setAttribute('height', String(p.size))
  const bg = doc.createElementNS(SVG_NS, 'rect')
  bg.setAttribute('width', String(p.size))
  bg.setAttribute('height', String(p.size))
  bg.setAttribute('style', `fill:${fill}`)
  pat.appendChild(bg)
  if (p.dot) {
    const c = doc.createElementNS(SVG_NS, 'circle')
    c.setAttribute('cx', String(p.size / 2))
    c.setAttribute('cy', String(p.size / 2))
    c.setAttribute('r', '1.1')
    c.setAttribute('style', 'fill:var(--oc-surface)')
    pat.appendChild(c)
  } else {
    const path = doc.createElementNS(SVG_NS, 'path')
    path.setAttribute('d', p.d)
    path.setAttribute('style', 'stroke:var(--oc-surface);stroke-width:1.2;fill:none')
    pat.appendChild(path)
  }
  defs.appendChild(pat)
}

function style(m: Mark, fill: string | undefined): string {
  const s: string[] = []
  const f = fill ?? m.fill ?? (m.type === 'text' ? 'var(--oc-ink-secondary)' : m.type === 'line' || m.type === 'path' ? 'none' : undefined)
  if (f) s.push(`fill:${f}`)
  if (m.stroke) s.push(`stroke:${m.stroke}`)
  if (m.strokeWidth !== undefined) s.push(`stroke-width:${m.strokeWidth}`)
  if (m.fillOpacity !== undefined) s.push(`fill-opacity:${m.fillOpacity}`)
  if (m.strokeOpacity !== undefined) s.push(`stroke-opacity:${m.strokeOpacity}`)
  if (m.dash) s.push(`stroke-dasharray:${m.dash}`)
  if (m.linecap) s.push(`stroke-linecap:${m.linecap}`)
  if (m.linejoin) s.push(`stroke-linejoin:${m.linejoin}`)
  if (m.inert) s.push('pointer-events:none')
  if (m.type === 'text') {
    s.push(`font-size:${m.size ?? 11}px`)
    if (m.weight) s.push(`font-weight:${m.weight}`)
    if (m.mono) s.push('font-family:var(--oc-font-mono)')
    if (m.anchor) s.push(`text-anchor:${m.anchor}`)
    if (m.baseline) s.push(`dominant-baseline:${m.baseline}`)
    if (m.halo) s.push('paint-order:stroke;stroke:var(--oc-surface);stroke-width:3px;stroke-linejoin:round')
  }
  return s.join(';')
}

export class SvgScene {
  readonly svg: SVGSVGElement
  readonly defs: SVGDefsElement
  readonly layers: Record<Layer, SVGGElement>
  private clip: SVGRectElement

  constructor(doc: Document, private opts: SceneOptions) {
    this.svg = doc.createElementNS(SVG_NS, 'svg')
    this.svg.setAttribute('class', 'oc-svg')
    this.defs = doc.createElementNS(SVG_NS, 'defs')
    this.svg.appendChild(this.defs)
    const cp = doc.createElementNS(SVG_NS, 'clipPath')
    cp.setAttribute('id', `oc-${opts.uid}-clip`)
    this.clip = doc.createElementNS(SVG_NS, 'rect')
    cp.appendChild(this.clip)
    this.defs.appendChild(cp)
    const layers = {} as Record<Layer, SVGGElement>
    for (const l of LAYERS) {
      const g = doc.createElementNS(SVG_NS, 'g')
      g.setAttribute('class', `oc-layer oc-${l}`)
      this.svg.appendChild(g)
      layers[l] = g
    }
    this.layers = layers
  }

  setOptions(o: Partial<SceneOptions>) {
    this.opts = { ...this.opts, ...o }
  }

  size(w: number, h: number) {
    this.svg.setAttribute('width', String(w))
    this.svg.setAttribute('height', String(h))
    this.svg.setAttribute('viewBox', `0 0 ${w} ${h}`)
  }

  setClip(rect: Rect | undefined) {
    const g = this.layers.marks
    if (!rect) {
      g.removeAttribute('clip-path')
      return
    }
    this.clip.setAttribute('x', String(rect.x))
    this.clip.setAttribute('y', String(rect.y))
    this.clip.setAttribute('width', String(Math.max(0, rect.width)))
    this.clip.setAttribute('height', String(Math.max(0, rect.height)))
    g.setAttribute('clip-path', `url(#oc-${this.opts.uid}-clip)`)
  }

  /** Render marks into their layers. `skip` layers are left empty (canvas draws them). */
  render(items: IndexedMark[], skip: ReadonlySet<Layer> = new Set()) {
    const byLayer = new Map<Layer, IndexedMark[]>(LAYERS.map((l) => [l, []]))
    for (const it of items) byLayer.get(it.mark.layer ?? 'marks')!.push(it)
    const { animate, duration } = this.opts
    for (const l of LAYERS) {
      const g = select(this.layers[l])
      const list = skip.has(l) ? [] : byLayer.get(l)!
      const sel = g.selectAll<SVGElement, IndexedMark>(':scope > *').data(list, (d) => (d ? markKey(d.mark, d.index) : ''))
      sel.exit().each(function () {
        const el = select(this as SVGElement)
        if (animate) el.interrupt().attr('data-exiting', '1').transition().duration(duration * 0.6).ease(ease).style('opacity', 0).remove()
        else el.remove()
      })
      const entered = sel.enter().append((d) => this.svg.ownerDocument.createElementNS(SVG_NS, TAG[d.mark.type]) as SVGElement)
      entered.each((d, i, nodes) => this.apply(nodes[i] as SVGElement, d, false))
      if (animate) entered.style('opacity', 0).transition().duration(duration).ease(ease).style('opacity', (d) => (d.mark.opacity ?? 1) as unknown as string)
      sel.each((d, i, nodes) => this.apply(nodes[i] as SVGElement, d, animate))
      // keep DOM order = mark order (paint order is meaningful)
      const merged = entered.merge(sel)
      merged.order()
    }
  }

  private apply(el: SVGElement, d: IndexedMark, transition: boolean) {
    const m = d.mark
    const s = select(el)
    s.attr('data-key', markKey(m, d.index))
    if (m.role) s.attr('data-role', m.role)
    else s.attr('data-role', null)
    if (m.datum !== undefined) s.attr('data-mark', String(d.index))
    else s.attr('data-mark', null)
    s.attr('data-series', m.series ?? null)
    s.attr('data-hover', m.hoverKey ?? null)
    s.attr('class', m.className ?? null)
    if (m.type === 'text') {
      el.textContent = m.text
      if (m.full) s.attr('data-full', m.full)
      else s.attr('data-full', null)
    }
    let fill = m.fill
    if (this.opts.patterns && m.pattern !== undefined && m.fill && m.type !== 'text' && m.type !== 'line') {
      const id = patternId(this.opts.uid, m.pattern, m.fill)
      ensurePattern(this.defs, id, m.pattern, m.fill)
      fill = `url(#${id})`
    }
    s.attr('style', style(m, fill))
    if (m.opacity !== undefined && !transition) s.style('opacity', String(m.opacity))
    const geo = geometry(m)
    const target = transition && !el.hasAttribute('data-exiting') ? s.interrupt().transition().duration(this.opts.duration).ease(ease) : null
    for (const [k, v] of Object.entries(geo)) {
      if (v === undefined) {
        s.attr(k, null)
        continue
      }
      if (target && k !== 'transform') target.attr(k, String(v))
      else s.attr(k, String(v))
    }
    // rotated text keeps its rotation after the mark's translate
    if (m.transform) s.attr('transform', m.type === 'text' && m.rotate ? `${m.transform} rotate(${m.rotate} ${r(m.x)} ${r(m.y)})` : m.transform)
    else if (m.type !== 'text' || !m.rotate) s.attr('transform', null)
    if (target && m.opacity !== undefined) target.style('opacity', String(m.opacity))
    else if (target) target.style('opacity', '1')
  }

  dim(key: string | null, by: 'series' | 'hover' = 'series') {
    const attr = by === 'series' ? 'data-series' : 'data-hover'
    select(this.layers.marks)
      .selectAll<SVGElement, unknown>(`[${attr}]`)
      .classed('oc-dim', function () {
        return key !== null && this.getAttribute(attr) !== key
      })
    if (key === null) select(this.layers.marks).selectAll('.oc-dim').classed('oc-dim', false)
  }

  destroy() {
    select(this.svg).selectAll('*').interrupt()
    this.svg.remove()
  }
}

/** The marks layer on a canvas, for large mark counts. */
export class CanvasScene {
  readonly canvas: HTMLCanvasElement
  private tree: Quadtree<IndexedMark> | null = null
  private items: IndexedMark[] = []

  constructor(doc: Document) {
    this.canvas = doc.createElement('canvas')
    this.canvas.className = 'oc-canvas'
    this.canvas.setAttribute('aria-hidden', 'true')
  }

  /** Paint; returns false when there is no 2D context (headless DOM). */
  draw(items: IndexedMark[], w: number, h: number, theme: ThemeName, clip: Rect | undefined, dimKey: string | null = null, dimBy: 'series' | 'hover' = 'series'): boolean {
    this.items = items
    this.tree = quadtree<IndexedMark>()
      .x((d) => anchor(d.mark)[0] + translateOf(d.mark.transform)[0])
      .y((d) => anchor(d.mark)[1] + translateOf(d.mark.transform)[1])
      .addAll(items.filter((d) => d.mark.datum !== undefined))
    const dpr = typeof window !== 'undefined' ? Math.max(1, Math.min(3, window.devicePixelRatio || 1)) : 1
    this.canvas.width = Math.round(w * dpr)
    this.canvas.height = Math.round(h * dpr)
    this.canvas.style.width = `${w}px`
    this.canvas.style.height = `${h}px`
    const c = this.canvas.getContext('2d')
    if (!c) return false
    c.setTransform(dpr, 0, 0, dpr, 0, 0)
    c.clearRect(0, 0, w, h)
    c.save()
    if (clip) {
      c.beginPath()
      c.rect(clip.x, clip.y, clip.width, clip.height)
      c.clip()
    }
    const paints = new Map<string, string>()
    const paint = (p: string | undefined) => {
      if (!p) return 'none'
      let v = paints.get(p)
      if (v === undefined) { v = resolvePaint(p, theme); paints.set(p, v) }
      return v
    }
    const order: Record<string, number> = { back: 0, grid: 1, marks: 2 }
    const sorted = [...items].sort((a, b) => (order[a.mark.layer ?? 'marks'] ?? 2) - (order[b.mark.layer ?? 'marks'] ?? 2) || a.index - b.index)
    for (const { mark: m } of sorted) {
      const own = dimBy === 'series' ? m.series : m.hoverKey
      const alpha = (m.opacity ?? 1) * (dimKey !== null && own !== undefined && own !== dimKey ? 0.22 : 1)
      const fill = paint(m.fill ?? (m.type === 'text' ? 'var(--oc-ink-secondary)' : undefined))
      const stroke = paint(m.stroke)
      const [tx, ty] = translateOf(m.transform)
      c.setTransform(dpr, 0, 0, dpr, tx * dpr, ty * dpr)
      c.globalAlpha = alpha
      c.setLineDash(m.dash ? m.dash.split(/[ ,]+/).map(Number) : [])
      c.lineWidth = m.strokeWidth ?? 1
      c.lineCap = m.linecap ?? 'butt'
      c.lineJoin = m.linejoin ?? 'miter'
      const doFill = (f: () => void) => {
        if (fill !== 'none' && fill !== 'transparent') { c.fillStyle = fill; c.globalAlpha = alpha * (m.fillOpacity ?? 1); f() }
      }
      const doStroke = (f: () => void) => {
        if (stroke !== 'none' && stroke !== 'transparent') { c.strokeStyle = stroke; c.globalAlpha = alpha * (m.strokeOpacity ?? 1); f() }
      }
      switch (m.type) {
        case 'rect':
          doFill(() => c.fillRect(m.x, m.y, m.width, m.height))
          doStroke(() => c.strokeRect(m.x, m.y, m.width, m.height))
          break
        case 'circle': {
          c.beginPath()
          c.arc(m.cx, m.cy, Math.max(0, m.r), 0, Math.PI * 2)
          doFill(() => c.fill())
          doStroke(() => c.stroke())
          break
        }
        case 'path': {
          const p = new Path2D(m.d)
          doFill(() => c.fill(p))
          doStroke(() => c.stroke(p))
          break
        }
        case 'line':
          c.beginPath()
          c.moveTo(m.x1, m.y1)
          c.lineTo(m.x2, m.y2)
          doStroke(() => c.stroke())
          break
        case 'text':
          c.font = `${m.weight ?? 400} ${m.size ?? 11}px ${m.mono ? FONT_MONO : FONT}`
          c.textAlign = m.anchor === 'middle' ? 'center' : m.anchor === 'end' ? 'right' : 'left'
          c.textBaseline = m.baseline === 'hanging' ? 'hanging' : m.baseline === 'middle' || m.baseline === 'central' ? 'middle' : 'alphabetic'
          doFill(() => c.fillText(m.text, m.x, m.y))
          break
      }
    }
    c.restore()
    c.setTransform(dpr, 0, 0, dpr, 0, 0)
    c.globalAlpha = 1
    return true
  }

  /** Nearest data mark within `radius` px. */
  hit(x: number, y: number, radius = 16): IndexedMark | null {
    return this.tree?.find(x, y, radius) ?? null
  }

  get count() {
    return this.items.length
  }

  destroy() {
    this.canvas.remove()
    this.tree = null
    this.items = []
  }
}

/** Sum of every translate(x[, y]) in a transform string (the only transform marks may carry). */
export function translateOf(t: string | undefined): [number, number] {
  if (!t) return [0, 0]
  let x = 0, y = 0
  for (const m of t.matchAll(/translate\(\s*(-?[\d.e+-]+)(?:[\s,]+(-?[\d.e+-]+))?\s*\)/g)) {
    x += +m[1]!
    y += +(m[2] ?? 0)
  }
  return [x, y]
}

/** A point that stands for a mark: tooltip anchor, hit-test centre. */
export function anchor(m: Mark): [number, number] {
  switch (m.type) {
    case 'rect': return [m.x + m.width / 2, m.y + m.height / 2]
    case 'circle': return [m.cx, m.cy]
    case 'path': return [m.cx ?? m.box?.x ?? 0, m.cy ?? m.box?.y ?? 0]
    case 'line': return [(m.x1 + m.x2) / 2, (m.y1 + m.y2) / 2]
    case 'text': return [m.x, m.y]
  }
}

/** Box for the focus ring. */
export function boxOf(m: Mark): Rect {
  switch (m.type) {
    case 'rect': return { x: m.x, y: m.y, width: Math.max(1, m.width), height: Math.max(1, m.height) }
    case 'circle': return { x: m.cx - m.r, y: m.cy - m.r, width: m.r * 2, height: m.r * 2 }
    case 'path': return m.box ?? { x: (m.cx ?? 0) - 6, y: (m.cy ?? 0) - 6, width: 12, height: 12 }
    case 'line': return { x: Math.min(m.x1, m.x2), y: Math.min(m.y1, m.y2), width: Math.max(2, Math.abs(m.x2 - m.x1)), height: Math.max(2, Math.abs(m.y2 - m.y1)) }
    case 'text': {
      // measured box from anchor and baseline (rotation ignored: the ring frames the anchor end)
      const size = m.size ?? 11, w = measure(m.text, size, m.weight ?? 400), h = size * 1.3
      const x = m.anchor === 'middle' ? m.x - w / 2 : m.anchor === 'end' ? m.x - w : m.x
      const y = m.baseline === 'hanging' ? m.y : m.baseline === 'middle' || m.baseline === 'central' ? m.y - h / 2 : m.y - size
      return { x, y, width: Math.max(4, w), height: h }
    }
  }
}

export type SvgSel = Selection<SVGSVGElement, unknown, null, undefined>
