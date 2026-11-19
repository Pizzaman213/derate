// Exports: a standalone SVG (theme paints resolved to literal colours, so it renders
// anywhere), a PNG rasterised from that SVG in the browser, and CSV from the table model.

import { csvFormatRows } from 'd3-dsv'
import { resolvePaint, FONT, type ThemeName } from './theme'
import type { TableModel } from './types'

const PAINT_PROPS = new Set(['fill', 'stroke', 'stop-color', 'color'])

function resolveStyle(style: string, theme: ThemeName): string {
  return style
    .split(';')
    .filter(Boolean)
    .map((decl) => {
      const i = decl.indexOf(':')
      if (i < 0) return decl
      const prop = decl.slice(0, i).trim(), val = decl.slice(i + 1).trim()
      if (PAINT_PROPS.has(prop)) return `${prop}:${resolvePaint(val, theme)}`
      if (prop === 'font-family' && val.includes('var(')) return `font-family:${FONT}`
      if (val.includes('var(--oc-surface)')) return `${prop}:${val.replace('var(--oc-surface)', resolvePaint('var(--oc-surface)', theme))}`
      return decl
    })
    .join(';')
}

/** Standalone SVG markup for the chart. */
export function exportSVG(svg: SVGSVGElement, opts: { theme: ThemeName; title: string; canvas?: HTMLCanvasElement | null; background?: boolean }): string {
  const clone = svg.cloneNode(true) as SVGSVGElement
  clone.querySelectorAll('.oc-focus-ring').forEach((n) => n.remove())
  clone.querySelectorAll('[data-exiting]').forEach((n) => n.remove())
  clone.removeAttribute('tabindex')
  clone.removeAttribute('aria-describedby')
  clone.removeAttribute('class')
  clone.setAttribute('style', `font-family:${FONT}`)
  const w = svg.getAttribute('width') ?? '0', h = svg.getAttribute('height') ?? '0'
  const doc = svg.ownerDocument
  clone.querySelectorAll('[style]').forEach((n) => n.setAttribute('style', resolveStyle(n.getAttribute('style') ?? '', opts.theme)))
  // defaults the stylesheet would have supplied
  clone.querySelectorAll('text').forEach((t) => {
    const s = t.getAttribute('style') ?? ''
    if (!/(^|;)fill:/.test(s)) t.setAttribute('style', `${s};fill:${resolvePaint('var(--oc-ink-secondary)', opts.theme)}`)
  })
  clone.querySelectorAll('.oc-dim').forEach((n) => (n as SVGElement).setAttribute('opacity', '0.22'))
  const title = doc.createElementNS('http://www.w3.org/2000/svg', 'title')
  title.textContent = opts.title
  clone.insertBefore(title, clone.firstChild)
  if (opts.background !== false) {
    const bg = doc.createElementNS('http://www.w3.org/2000/svg', 'rect')
    bg.setAttribute('width', w)
    bg.setAttribute('height', h)
    bg.setAttribute('style', `fill:${resolvePaint('var(--oc-surface)', opts.theme)}`)
    clone.insertBefore(bg, title.nextSibling)
  }
  if (opts.canvas) {
    let url = ''
    try { url = opts.canvas.toDataURL('image/png') ?? '' } catch { url = '' }
    if (url.startsWith('data:image/png')) {
      const img = doc.createElementNS('http://www.w3.org/2000/svg', 'image')
      img.setAttribute('href', url)
      img.setAttribute('width', w)
      img.setAttribute('height', h)
      const marks = clone.querySelector('.oc-marks')
      marks?.parentNode?.insertBefore(img, marks)
    }
  }
  return new XMLSerializer().serializeToString(clone)
}

/** PNG of the exported SVG at `scale`x; browser only. */
export function exportPNG(svgText: string, width: number, height: number, scale = 2): Promise<Blob> {
  return new Promise((resolve, reject) => {
    if (typeof Image === 'undefined' || typeof document === 'undefined') return reject(new Error('exportPNG needs a browser (Image + canvas)'))
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(width * scale)
    canvas.height = Math.round(height * scale)
    const ctx = canvas.getContext('2d')
    if (!ctx) return reject(new Error('exportPNG needs a 2D canvas; this environment has none'))
    const img = new Image()
    const url = URL.createObjectURL(new Blob([svgText], { type: 'image/svg+xml;charset=utf-8' }))
    img.onload = () => {
      ctx.setTransform(scale, 0, 0, scale, 0, 0)
      ctx.drawImage(img, 0, 0)
      URL.revokeObjectURL(url)
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('canvas.toBlob returned nothing'))), 'image/png')
    }
    img.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new Error('the SVG could not be rasterised'))
    }
    img.src = url
  })
}

/** RFC 4180 CSV of the table model. */
export function exportCSV(model: TableModel): string {
  return csvFormatRows([model.columns, ...model.rows.map((r) => r.map((c) => (c === null || c === undefined ? '' : String(c))))])
}
