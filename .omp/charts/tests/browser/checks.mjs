// In-page layout checks, run with page.evaluate(layoutCheck, hostSelector).
// Returns a list of findings (empty = pass). Real layout only: these are what jsdom
// cannot see -- overflow, clipped text, overlapping axis labels, legend fit.

export function layoutCheck(sel) {
  // helpers live inside: this function is serialised into the page
  const rotation = (t) => { const m = (t.getAttribute('transform') || '').match(/rotate\((-?[\d.]+)/); return m ? +m[1] : 0 }
  // screen position of the text's anchor point: rotate(a x y) keeps (x, y) fixed, translates move it
  const anchorOf = (t) => { const p = new DOMPoint(+t.getAttribute('x'), +t.getAttribute('y')).matrixTransform(t.getScreenCTM()); return { x: p.x, y: p.y } }
  const findings = []
  const host = document.querySelector(sel)
  if (!host) return ['host not found']
  const root = host.querySelector('.oc-root')
  if (!root) return ['no .oc-root rendered']
  const hr = host.getBoundingClientRect()
  const tol = 1.5
  const inside = (r, box) => r.left >= box.left - tol && r.right <= box.right + tol && r.top >= box.top - tol && r.bottom <= box.bottom + tol
  const name = (el) => `${el.tagName.toLowerCase()}${el.getAttribute('data-key') ? `[${el.getAttribute('data-key')}]` : ''}${el.className && typeof el.className === 'string' ? '.' + el.className.split(' ')[0] : ''} "${(el.textContent || '').slice(0, 30)}"`
  // 1. nothing overflows the container
  if (root.scrollWidth > root.clientWidth + tol) findings.push(`root overflows horizontally: scrollWidth ${root.scrollWidth} > clientWidth ${root.clientWidth}`)
  if (host.scrollWidth > host.clientWidth + tol) findings.push(`host overflows horizontally: ${host.scrollWidth} > ${host.clientWidth}`)
  const rr = root.getBoundingClientRect()
  if (rr.width > hr.width + tol) findings.push(`chart is wider than its container: ${rr.width} > ${hr.width}`)
  const svg = root.querySelector('svg.oc-svg')
  if (svg && !svg.closest('[hidden]')) {
    const sr = svg.getBoundingClientRect()
    if (sr.right > hr.right + tol) findings.push(`svg extends past the container: ${sr.right} > ${hr.right}`)
    // 2. every text inside the svg box (= not clipped)
    const texts = [...svg.querySelectorAll('text')].filter((t) => t.textContent && t.getAttribute('visibility') !== 'hidden' && getComputedStyle(t).display !== 'none' && !t.closest('[data-exiting]'))
    for (const t of texts) {
      const r = t.getBoundingClientRect()
      if (r.width === 0 && r.height === 0) continue
      if (!inside(r, sr)) findings.push(`text clipped by the svg edge: ${name(t)} at [${r.left.toFixed(0)},${r.top.toFixed(0)},${r.right.toFixed(0)},${r.bottom.toFixed(0)}] vs svg [${sr.left.toFixed(0)},${sr.top.toFixed(0)},${sr.right.toFixed(0)},${sr.bottom.toFixed(0)}]`)
    }
    // 3. axis labels do not overlap each other
    const ax = [...svg.querySelectorAll('.oc-axes text')].map((t) => ({ t, r: t.getBoundingClientRect() })).filter((x) => x.r.width > 0)
    for (let i = 0; i < ax.length; i++)
      for (let j = i + 1; j < ax.length; j++) {
        const a = ax[i].r, b = ax[j].r
        const ix = Math.min(a.right, b.right) - Math.max(a.left, b.left)
        const iy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)
        if (ix <= 1 || iy <= 1) continue
        // rotated by the same angle: the axis-aligned boxes are loose; measure the gap
        // between the two baselines along the text's normal instead
        const ra = rotation(ax[i].t), rb = rotation(ax[j].t)
        if (ra !== 0 && ra === rb) {
          const th = (ra * Math.PI) / 180
          const nx = -Math.sin(th), ny = Math.cos(th)
          const pa = anchorOf(ax[i].t), pb = anchorOf(ax[j].t)
          const gap = Math.abs((pb.x - pa.x) * nx + (pb.y - pa.y) * ny)
          if (gap >= parseFloat(getComputedStyle(ax[i].t).fontSize) * 0.9) continue
        } else if (ra !== rb) {
          // one rotated: test the upright label's corners against the rotated label's real (oriented) box
          const [rt, up] = ra !== 0 ? [ax[i].t, ax[j]] : [ax[j].t, ax[i]]
          const th = (rotation(rt) * Math.PI) / 180
          const dx = Math.cos(th), dy = Math.sin(th), nx = -dy, ny = dx
          const p = anchorOf(rt)
          const L = rt.getComputedTextLength(), fs = parseFloat(getComputedStyle(rt).fontSize)
          const anchor = getComputedStyle(rt).textAnchor
          const [lo, hi] = anchor === 'end' ? [-L, 0] : anchor === 'middle' ? [-L / 2, L / 2] : [0, L]
          const u = up.r
          const corners = [[u.left, u.top], [u.right, u.top], [u.left, u.bottom], [u.right, u.bottom], [(u.left + u.right) / 2, (u.top + u.bottom) / 2]]
          const hit = corners.some(([cx, cy]) => { const vx = cx - p.x, vy = cy - p.y; const along = vx * dx + vy * dy, across = vx * nx + vy * ny; return along > lo + 1 && along < hi - 1 && Math.abs(across) < fs / 2 - 1 })
          if (!hit) continue
        }
        findings.push(`axis labels overlap: ${name(ax[i].t)} and ${name(ax[j].t)}`)
      }
  }
  // 4. HTML text not clipped (ellipsis with a title is intentional truncation)
  for (const el of root.querySelectorAll('.oc-title, .oc-note, .oc-legend .oc-lt, .oc-legend-title, .oc-gradient span, .oc-state strong, .oc-state li')) {
    if (el.closest('[hidden]')) continue
    const cs = getComputedStyle(el)
    const truncatedOnPurpose = cs.textOverflow === 'ellipsis' && !!(el.closest('[title]'))
    if (el.scrollWidth > el.clientWidth + tol && !truncatedOnPurpose) findings.push(`text clipped: ${name(el)} scrollWidth ${el.scrollWidth} > clientWidth ${el.clientWidth}`)
  }
  // 5. legend fits or wraps
  const legend = root.querySelector('.oc-legend')
  if (legend && !legend.hidden) {
    const lr = legend.getBoundingClientRect()
    if (lr.right > hr.right + tol) findings.push(`legend extends past the container: ${lr.right} > ${hr.right}`)
    for (const it of legend.querySelectorAll('li')) if (!inside(it.getBoundingClientRect(), { ...lr.toJSON(), left: lr.left, right: lr.right, top: lr.top, bottom: lr.bottom })) findings.push(`legend item outside the legend: ${name(it)}`)
  }
  return findings
}

/** Centre of data mark i in viewport coordinates, or null (svg path only). */
export function markCenter([sel, i]) {
  const host = document.querySelector(sel)
  const marks = [...host.querySelectorAll('svg.oc-svg [data-mark]')].filter((m) => { const r = m.getBoundingClientRect(); return r.width + r.height > 0 })
  const m = marks[i]
  if (!m) return null
  const r = m.getBoundingClientRect()
  const hits = (x, y) => { const e = document.elementFromPoint(x, y); return e === m || m.contains(e) }
  const c = { x: r.left + r.width / 2, y: r.top + r.height / 2 }
  if (hits(c.x, c.y)) return c
  // ring arcs, thin strokes: the box centre can be empty space; find a point on the mark
  for (let gy = 1; gy < 12; gy++) for (let gx = 1; gx < 12; gx++) {
    const x = r.left + (r.width * gx) / 12, y = r.top + (r.height * gy) / 12
    if (hits(x, y)) return { x, y }
  }
  return c
}

/** The tooltip must be visible and inside the viewport. */
export function tooltipCheck(sel) {
  const host = document.querySelector(sel)
  const tip = host.querySelector('.oc-tooltip')
  if (!tip || tip.hidden) return ['tooltip did not appear on hover']
  const r = tip.getBoundingClientRect()
  const f = []
  if (r.left < 0 || r.top < 0 || r.right > window.innerWidth || r.bottom > window.innerHeight) f.push(`tooltip leaves the viewport: [${r.left},${r.top},${r.right},${r.bottom}] in ${window.innerWidth}x${window.innerHeight}`)
  if (!tip.textContent.trim()) f.push('tooltip is empty')
  return f
}
