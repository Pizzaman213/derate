// Things laid out in one row sit on one line.
//
// The screens verifier runs this inside every page it opens, after the
// screenshot. It exists because of /requests: its filter bar is one flex row
// with `align-items: center`, and the window chips (`.chips`, which carries a
// bottom margin for its usual home above a grid) were centred by their MARGIN
// box, so 24h/7d/30d hung 5px above the selects beside them -- the row
// "bounced". Nothing in typecheck, a pure verifier or a screenshot-exists
// assertion can see that; a measurement of the rendered boxes can.
//
// Two rules, both measured off getBoundingClientRect in the real layout:
//
//   Controls on one line of a flex row share a centre line. Per flex row
//   (display flex|inline-flex, direction row|row-reverse), each item
//   contributes its form controls -- itself when it is one, else the controls
//   inside it, if those sit on one line (an item whose controls stack is a
//   column, and is judged as its own container instead). Items are grouped
//   into the row's wrapped lines; on a line with two or more controls, every
//   control's vertical centre is within TOLERANCE of the line's median.
//   Checkboxes and radios are not controls here: they align to their label's
//   text, by design.
//
//   Cells of one table row share a text baseline. The first visible text of
//   each cell is measured with a Range; its baseline is the rect's top plus
//   the font's ascent (canvas measureText on the computed font). A cell whose
//   second line pushes the row taller must not drag the other cells' first
//   line off the row's.
//
// Pure DOM, no imports and no closure over this module: Playwright serialises
// the function's source into the page.
//
//   const misaligned = await page.evaluate(alignmentAudit)
//   // -> ['div > span.chips button "24h": centre 5.0px off its line (…)', …]

export function alignmentAudit(tolerance = 1) {
  const CONTROL =
    'button, select, textarea, [role="button"], input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"])'
  const out = []

  const shown = (el) => {
    const r = el.getBoundingClientRect()
    if (r.width <= 0 || r.height <= 0) return false
    const cs = getComputedStyle(el)
    return cs.visibility !== 'hidden' && cs.display !== 'none' && Number(cs.opacity) > 0
  }
  const name = (el) => {
    const cls = typeof el.className === 'string' ? el.className.split(/\s+/).filter(Boolean).slice(0, 2) : []
    return el.tagName.toLowerCase() + (el.id ? `#${el.id}` : '') + (cls.length ? `.${cls.join('.')}` : '')
  }
  const label = (el) => {
    const t = (el.getAttribute('aria-label') || el.textContent || el.getAttribute('placeholder') || '').trim()
    return t ? ` "${t.slice(0, 24)}"` : ''
  }
  const where = (el) => {
    const parts = []
    for (let p = el; p && p !== document.body && parts.length < 3; p = p.parentElement) parts.unshift(name(p))
    return parts.join(' > ')
  }
  const centre = (r) => r.top + r.height / 2
  // Vertical ranges that overlap by more than a pixel are one line.
  const oneLine = (rects) => {
    const top = Math.max(...rects.map((r) => r.top))
    const bottom = Math.min(...rects.map((r) => r.bottom))
    return bottom - top > 1
  }
  const median = (xs) => {
    const s = [...xs].sort((a, b) => a - b)
    const m = s.length >> 1
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
  }

  // ---- controls on one line of a flex row share a centre ------------------
  // Rows nest (a chip group is a row inside the bar's row); a control off its
  // line is named once, by the outermost row that sees it.
  const named = new Set()
  for (const row of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(row)
    if (!/flex$/.test(cs.display) || cs.flexDirection.startsWith('column') || !shown(row)) continue

    const items = []
    for (const item of row.children) {
      if (!shown(item)) continue
      const pos = getComputedStyle(item).position
      if (pos === 'absolute' || pos === 'fixed') continue
      const controls = item.matches(CONTROL)
        ? [item]
        : [...item.querySelectorAll(CONTROL)].filter(shown)
      if (controls.length === 0) continue
      const rects = controls.map((c) => c.getBoundingClientRect())
      if (!oneLine(rects)) continue
      items.push({ box: item.getBoundingClientRect(), controls, rects })
    }
    if (items.length < 2 && !(items.length === 1 && items[0].controls.length > 1)) continue

    // A wrapping row is several lines; an item joins the line its box overlaps.
    items.sort((a, b) => a.box.top - b.box.top)
    const lines = []
    for (const it of items) {
      const line = lines.find((l) => oneLine([l.box, it.box]))
      if (line) {
        line.members.push(it)
        line.box = { top: Math.min(line.box.top, it.box.top), bottom: Math.max(line.box.bottom, it.box.bottom) }
      } else lines.push({ box: { top: it.box.top, bottom: it.box.bottom }, members: [it] })
    }

    for (const line of lines) {
      const all = line.members.flatMap((m) => m.controls.map((c, i) => ({ el: c, r: m.rects[i] })))
      if (all.length < 2) continue
      const mid = median(all.map((c) => centre(c.r)))
      for (const c of all) {
        const off = centre(c.r) - mid
        if (Math.abs(off) > tolerance && !named.has(c.el)) {
          named.add(c.el)
          out.push(
            `${where(c.el)}${label(c.el)}: centre ${Math.abs(off).toFixed(1)}px ${off < 0 ? 'above' : 'below'} ` +
              `its line in ${name(row)} (${Math.round(c.r.height)}px tall, line of ${all.length} controls)`,
          )
        }
      }
    }
  }

  // ---- cells of one table row share a baseline ----------------------------
  const ctx = document.createElement('canvas').getContext('2d')
  const firstText = (cell) => {
    const walk = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) =>
        n.textContent.trim() && n.parentElement && shown(n.parentElement)
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_REJECT,
    })
    return walk.nextNode()
  }
  for (const tr of document.querySelectorAll('tr')) {
    if (!shown(tr)) continue
    const lines = []
    for (const cell of tr.cells) {
      if (cell.colSpan > 1 && tr.cells.length === 1) continue
      const node = firstText(cell)
      if (!node) continue
      const range = document.createRange()
      range.selectNodeContents(node)
      const rect = range.getClientRects()[0]
      if (!rect) continue
      ctx.font = getComputedStyle(node.parentElement).font
      const ascent = ctx.measureText(node.textContent.trim()).fontBoundingBoxAscent
      lines.push({ cell, base: rect.top + ascent })
    }
    if (lines.length < 2) continue
    const mid = median(lines.map((l) => l.base))
    for (const l of lines) {
      const off = l.base - mid
      if (Math.abs(off) > tolerance) {
        out.push(
          `${where(l.cell)}${label(l.cell)}: baseline ${Math.abs(off).toFixed(1)}px ${off < 0 ? 'above' : 'below'} ` +
            `the rest of its table row`,
        )
      }
    }
  }

  return out
}
