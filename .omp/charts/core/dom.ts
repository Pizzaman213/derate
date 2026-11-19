// The HTML parts around the SVG: title, legend, tooltip, live region, data table,
// note, and the empty / error states.

import type { LegendSpec, TableModel, TooltipContent, ValidationIssue } from './types'

export function el<K extends keyof HTMLElementTagNameMap>(doc: Document, tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const e = doc.createElement(tag)
  if (cls) e.className = cls
  if (text !== undefined) e.textContent = text
  return e
}

/** Legend: toggle buttons (aria-pressed) or plain items, plus an optional gradient. */
export function renderLegend(
  doc: Document,
  host: HTMLElement,
  spec: LegendSpec | undefined,
  hidden: ReadonlySet<string>,
): void {
  host.replaceChildren()
  if (!spec || (!spec.items?.length && !spec.gradient)) {
    host.hidden = true
    return
  }
  host.hidden = false
  if (spec.title) host.appendChild(el(doc, 'li', 'oc-legend-title', spec.title))
  const toggle = spec.toggle ?? (spec.items?.length ?? 0) > 1
  for (const it of spec.items ?? []) {
    const li = el(doc, 'li')
    const b = toggle ? el(doc, 'button') : el(doc, 'span', 'oc-li')
    if (toggle) {
      ;(b as HTMLButtonElement).type = 'button'
      b.setAttribute('aria-pressed', String(!hidden.has(it.key)))
    }
    b.setAttribute('data-key', it.key)
    b.title = it.label
    const sw = el(doc, 'span', `oc-sw${it.shape && it.shape !== 'square' ? ` oc-sw-${it.shape}` : ''}`)
    sw.style.background = it.color
    sw.style.borderColor = it.color
    sw.setAttribute('aria-hidden', 'true')
    b.append(sw, el(doc, 'span', 'oc-lt', it.label))
    li.appendChild(b)
    host.appendChild(li)
  }
  if (spec.gradient) {
    const li = el(doc, 'li', 'oc-gradient')
    const bar = el(doc, 'span', 'oc-bar')
    bar.style.background = `linear-gradient(to right, ${spec.gradient.stops.join(', ')})`
    bar.setAttribute('aria-hidden', 'true')
    if (spec.gradient.mid !== undefined) {
      // diverging: the centre value sits under the middle of the bar
      const wrap = el(doc, 'span', 'oc-bar-wrap')
      const mid = el(doc, 'span', 'oc-bar-mid', spec.gradient.mid)
      wrap.append(bar, mid)
      li.append(el(doc, 'span', undefined, spec.gradient.min), wrap, el(doc, 'span', undefined, spec.gradient.max))
    } else li.append(el(doc, 'span', undefined, spec.gradient.min), bar, el(doc, 'span', undefined, spec.gradient.max))
    host.appendChild(li)
  }
}

/** Tooltip body. */
export function fillTooltip(doc: Document, tip: HTMLElement, c: TooltipContent | string): void {
  tip.replaceChildren()
  if (typeof c === 'string') {
    tip.textContent = c
    return
  }
  if (c.title) {
    const t = el(doc, 'div', 'oc-tt-title')
    if (c.color) {
      const sw = el(doc, 'span', 'oc-tt-sw')
      sw.style.background = c.color
      t.appendChild(sw)
    }
    t.appendChild(doc.createTextNode(c.title))
    tip.appendChild(t)
  }
  if (c.rows?.length) {
    const table = el(doc, 'table')
    for (const [k, v] of c.rows) {
      const tr = el(doc, 'tr')
      tr.append(el(doc, 'td', undefined, k), el(doc, 'td', 'oc-v', v))
      table.appendChild(tr)
    }
    tip.appendChild(table)
  }
}

/** Place the tooltip near (x, y) in viewport coordinates, flipped and clamped to stay on screen. */
export function placeTooltip(tip: HTMLElement, x: number, y: number): void {
  tip.hidden = false
  const vw = window.innerWidth || 1024, vh = window.innerHeight || 768
  const w = tip.offsetWidth || 160, h = tip.offsetHeight || 40
  let left = x + 12, top = y - h - 10
  if (left + w > vw - 4) left = x - w - 12
  if (top < 4) top = y + 14
  left = Math.max(4, Math.min(vw - w - 4, left))
  top = Math.max(4, Math.min(vh - h - 4, top))
  tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`
}

const TABLE_CAP = 500
/** The data table: hidden for screen readers unless `visible`. */
export function renderTable(doc: Document, host: HTMLTableElement, id: string, caption: string, model: TableModel, visible: boolean): void {
  host.replaceChildren()
  host.id = id
  // a table cannot be shrunk below its content, so the visually-hidden class goes on its wrapper
  host.className = 'oc-table'
  if (host.parentElement) host.parentElement.className = visible ? 'oc-table-wrap' : 'oc-table-wrap oc-sr'
  host.appendChild(el(doc, 'caption', undefined, caption))
  const thead = el(doc, 'thead')
  const hr = el(doc, 'tr')
  for (const c of model.columns) {
    const th = el(doc, 'th', undefined, c)
    th.scope = 'col'
    hr.appendChild(th)
  }
  thead.appendChild(hr)
  const tbody = el(doc, 'tbody')
  for (const row of model.rows.slice(0, TABLE_CAP)) {
    const tr = el(doc, 'tr')
    for (const cell of row) tr.appendChild(el(doc, 'td', undefined, cell === null || cell === undefined ? '–' : String(cell)))
    tbody.appendChild(tr)
  }
  if (model.rows.length > TABLE_CAP) {
    const tr = el(doc, 'tr')
    const td = el(doc, 'td', undefined, `… ${model.rows.length - TABLE_CAP} more rows; export CSV for all of them`)
    td.colSpan = Math.max(1, model.columns.length)
    tr.appendChild(td)
    tbody.appendChild(tr)
  }
  host.append(thead, tbody)
}

/** Empty or error plate: never a blank box. */
export function renderState(doc: Document, host: HTMLElement, kind: 'empty' | 'error', name: string, message: string, errors: ValidationIssue[] = []): void {
  host.replaceChildren()
  host.className = `oc-state${kind === 'error' ? ' oc-error' : ''}`
  if (kind === 'error') {
    host.setAttribute('role', 'alert')
    host.removeAttribute('aria-label')
    host.appendChild(el(doc, 'strong', undefined, `${name}: can't draw this data`))
    const ul = el(doc, 'ul')
    for (const e of errors.slice(0, 5)) ul.appendChild(el(doc, 'li', undefined, e.message))
    if (errors.length > 5) ul.appendChild(el(doc, 'li', undefined, `…and ${errors.length - 5} more`))
    host.appendChild(ul)
  } else {
    host.setAttribute('role', 'img')
    host.setAttribute('aria-label', `${name}: ${message}`)
    host.appendChild(el(doc, 'strong', undefined, message))
  }
}
