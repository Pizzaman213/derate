// requires: browser
// Layout, overflow, hit targets, contrast and motion, for every gallery state
// across theme × viewport × stress. Screenshots are CAPTURED to
// tests/ui/screens/<component>/<variant>-<state>-<theme>-<w>[-<stress>].png
// and never pixel-diffed (baselines are machine-dependent); the in-page
// assertions are the gate:
//
//   - no horizontal page scroll (documentElement.scrollWidth <= innerWidth)
//   - no element whose rect escapes its nearest overflow:hidden|clip ancestor
//     or the viewport
//   - no text element with scrollWidth > clientWidth whose overflow is not
//     auto|scroll (clipped text)
//   - no two interactive elements with intersecting rects
//   - every interactive rect ≥ 24×24 (fail); a note lists those < 44
//   - contrast: for every visible text node, the computed colour against the
//     first non-transparent ancestor background (alpha composited over
//     --panel) is ≥ 4.5:1, or ≥ 3:1 at ≥ 24px / bold ≥ 18.66px, and ≥ 7 / 3
//     in high-contrast. This is COMPUTED-STYLE contrast, not screenshot
//     sampling: it cannot see gradients, images or text over a chart.
//   - under reducedMotion: 'reduce', document.getAnimations() has nothing
//     with a duration above 1 ms.
//
// The matrix is 3 themes × 4 widths × 8 stresses, minus zoom at 320/768: CSS
// `zoom: 2` halves the CSS viewport without re-evaluating the media queries
// the way real browser zoom does, so a 440px card inside a 384-CSS-px viewport
// reports an overflow the product never shows -- zoom runs at 1280 and 1920
// only. Cells run three at a time in separate contexts of one browser; the
// per-section screenshot is the proof a person can look at.
//
//   cd ui && node ../.omp/tests/ui/layout.check.mjs [--only=Readout] [--quick]

import { mkdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { report } from '../../../ui/src/check/harness.mjs'
import { openGallery, launch, THEMES, VIEWPORTS } from './lib/browser.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const SCREENS = join(here, 'screens')
const args = process.argv.slice(2)
// --only= when run by hand; DERATE_GALLERY_ONLY when run.mjs scopes it through ui/check.mjs,
// which launches every verifier with no arguments.
const only = (args.find((a) => a.startsWith('--only=')) ?? '').slice(7) || process.env.DERATE_GALLERY_ONLY || null
const quick = args.includes('--quick')
const STRESSES = quick ? ['none'] : ['none', 'long', 'url', 'rtl', 'emoji', 'empty', 'zoom', 'font']
const widths = quick ? [320, 1280] : VIEWPORTS

const { check, note, done } = report()
const browser = await launch()

// Runs in the page: every assertion, returning findings as strings.
const AUDIT = `(() => {
  const out = { overflowX: false, spill: [], escapes: [], clipped: [], overlaps: [], small: [], tiny: [], contrast: [] }
  const vw = window.innerWidth, vh = document.documentElement.scrollHeight
  out.overflowX = document.documentElement.scrollWidth > window.innerWidth + 1
  const visible = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' }
  const clipAncestor = (el) => { let p = el.parentElement; while (p) { const o = getComputedStyle(p).overflow; if (/hidden|clip/.test(o)) return p; p = p.parentElement } return null }
  // Content inside an overflow:auto|scroll ancestor is reachable by scrolling; that is containment, not escape.
  const scrollAncestor = (el) => { let p = el.parentElement; while (p) { const cs = getComputedStyle(p); if (/auto|scroll/.test(cs.overflowX) || /auto|scroll/.test(cs.overflow)) return p; p = p.parentElement } return null }
  const name = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' ? '.' + el.className.split(' ').filter(Boolean).slice(0, 2).join('.') : '') + ' in ' + (el.closest('[data-gallery]')?.dataset.component ?? '?') + '/' + (el.closest('[data-gallery]')?.dataset.state ?? '?')
  // Screen-reader-only content (the sr-only pattern: a ≤1px box that clips its
  // overflow, e.g. the chart pack's hidden data table) is not on screen, so it
  // cannot escape or overlap anything visually. Computed size, not the rect:
  // under the zoom stress a 1px box measures 2px.
  const srOnly = (el) => { for (let p = el; p; p = p.parentElement) { const cs = getComputedStyle(p); if (parseFloat(cs.width) <= 1 && parseFloat(cs.height) <= 1 && /hidden|clip/.test(cs.overflow)) return true } return false }
  const all = [...document.querySelectorAll('[data-gallery] *')].filter((el) => visible(el) && !srOnly(el))
  for (const el of all) {
    const r = el.getBoundingClientRect()
    if ((r.right > vw + 1 || r.left < -1) && !scrollAncestor(el)) out.escapes.push(name(el) + ' x ' + Math.round(r.left) + '…' + Math.round(r.right) + ' vs viewport ' + vw)
    const anc = clipAncestor(el)
    if (anc && !el.closest('.sheet')) { const a = anc.getBoundingClientRect(); if (r.right > a.right + 1 || r.left < a.left - 1 || r.bottom > a.bottom + 1) out.escapes.push(name(el) + ' escapes clipping ancestor ' + anc.tagName.toLowerCase()) }
    const cs = getComputedStyle(el)
    const hasText = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())
    if (hasText && el.scrollWidth > el.clientWidth + 1 && !/auto|scroll/.test(cs.overflowX) && !/auto|scroll/.test(cs.overflow) && cs.overflow !== 'visible') out.clipped.push(name(el) + ' scrollWidth ' + el.scrollWidth + ' > ' + el.clientWidth)
    // text wider than its box with overflow visible spills into the page: the usual cause of a horizontal scrollbar
    if (hasText && el.scrollWidth > el.clientWidth + 1 && cs.overflow === 'visible' && !scrollAncestor(el)) out.spill.push(name(el) + ' text ' + el.scrollWidth + 'px wide in a ' + el.clientWidth + 'px box')
  }
  const inter = [...document.querySelectorAll('[data-gallery] button, [data-gallery] a[href], [data-gallery] input, [data-gallery] select, [data-gallery] textarea, [data-gallery] [role=button], [data-gallery] [tabindex]:not([tabindex="-1"])')].filter(visible)
  const rects = inter.map((el) => ({ el, r: el.getBoundingClientRect() }))
  for (let i = 0; i < rects.length; i++) {
    const { el, r } = rects[i]
    // Native checkboxes and radios are UA-sized controls (WCAG 2.5.8's user-agent exception).
    if (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) { if (r.width < 13 || r.height < 13) out.small.push(name(el)); continue }
    if (r.width < 24 || r.height < 24) out.small.push(name(el) + ' ' + Math.round(r.width) + '×' + Math.round(r.height))
    else if (r.width < 44 || r.height < 44) out.tiny.push(name(el) + ' ' + Math.round(r.width) + '×' + Math.round(r.height))
    for (let j = i + 1; j < rects.length; j++) {
      const b = rects[j]
      if (el.contains(b.el) || b.el.contains(el)) continue
      const ix = Math.min(r.right, b.r.right) - Math.max(r.left, b.r.left), iy = Math.min(r.bottom, b.r.bottom) - Math.max(r.top, b.r.top)
      if (ix > 1 && iy > 1) out.overlaps.push(name(el) + ' ∩ ' + name(b.el))
    }
  }
  // contrast
  const parse = (c) => { const m = c.match(/rgba?\\(([\\d.]+),\\s*([\\d.]+),\\s*([\\d.]+)(?:,\\s*([\\d.]+))?\\)/); return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : null }
  const lum = ([r, g, b]) => { const f = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4 }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b) }
  const comp = (fg, bg) => [0, 1, 2].map((i) => fg[i] * fg[3] + bg[i] * (1 - fg[3])).concat([1])
  const panel = parse(getComputedStyle(document.body).backgroundColor) ?? [255, 255, 255, 1]
  const bgOf = (el) => { let p = el; while (p && p !== document.documentElement) { const c = parse(getComputedStyle(p).backgroundColor); if (c && c[3] > 0) return c[3] < 1 ? comp(c, panel) : c; if (getComputedStyle(p).backgroundImage !== 'none') return null; p = p.parentElement } return panel }
  const hc = document.documentElement.getAttribute('data-theme') === 'high-contrast'
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  const seen = new Set()
  let n
  while ((n = walker.nextNode())) {
    if (!n.textContent.trim()) continue
    const el = n.parentElement
    if (!el || !el.closest('[data-gallery]') || seen.has(el) || !visible(el)) continue
    seen.add(el)
    if (el.closest('svg')) continue
    const cs = getComputedStyle(el)
    const fg = parse(cs.color); const bg = bgOf(el)
    if (!fg || !bg) continue
    const f = fg[3] < 1 ? comp(fg, bg) : fg
    const l1 = lum(f), l2 = lum(bg)
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
    const size = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight, 10) >= 700
    const large = size >= 24 || (bold && size >= 18.66)
    const opacity = (() => { let o = 1, p = el; while (p && p !== document.body) { o *= parseFloat(getComputedStyle(p).opacity); p = p.parentElement } return o })()
    if (opacity < 0.9) continue // dimmed rows are a deliberate state, measured elsewhere
    const min = hc ? (large ? 3 : 7) : large ? 3 : 4.5
    if (ratio < min) out.contrast.push(name(el) + ' ' + ratio.toFixed(2) + ':1 < ' + min + ' (' + cs.color + ' on rgb(' + bg.slice(0, 3).map(Math.round).join(',') + ') "' + n.textContent.trim().slice(0, 24) + '")')
  }
  return out
})()`

const plan = []
for (const theme of THEMES) for (const w of widths) for (const stress of STRESSES) {
  if (stress === 'zoom' && w < 1280) continue
  plan.push({ theme, w, stress })
}
const results = []
let cells = 0
const runCell = async ({ theme, w, stress }) => {
      const q = new URLSearchParams({ theme })
      if (stress === 'zoom') q.set('zoom', '2')
      else if (stress === 'font') q.set('font', 'large')
      else if (stress !== 'none') q.set('stress', stress)
      if (only) q.set('only', only)
      const g = await openGallery({ query: q.toString(), viewport: { width: w, height: 900 }, browser, reducedMotion: 'reduce' })
      const { page } = g
      try {
        await page.waitForTimeout(150)
        const r = await page.evaluate(AUDIT)
        const anim = await page.evaluate(() => document.getAnimations().filter((a) => (a.effect?.getTiming().duration ?? 0) > 1).length)
        results.push({ label: `${theme} ${w}px ${stress}`, r, anim, errors: [...g.errors] })
        cells++
        // screenshots, per section
        const secs = await page.$$('[data-gallery]')
        for (const s of secs) {
          const d = await s.evaluate((e) => e.dataset)
          const dir = join(SCREENS, d.component)
          mkdirSync(dir, { recursive: true })
          await s.screenshot({ path: join(dir, `${d.variant}-${d.state}-${theme}-${w}${stress !== 'none' ? '-' + stress : ''}.png`.replace(/[^A-Za-z0-9._-]/g, '_')) }).catch(() => {})
        }
      } finally {
        await g.close()
      }
}
// three cells at a time
const queue = [...plan]
await Promise.all(Array.from({ length: 3 }, async () => { while (queue.length) await runCell(queue.shift()) }))
await browser.close()
// report in plan order so a diff between runs is readable
for (const { label, r, anim, errors } of results.sort((a, b) => plan.findIndex((p) => `${p.theme} ${p.w}px ${p.stress}` === a.label) - plan.findIndex((p) => `${p.theme} ${p.w}px ${p.stress}` === b.label))) {
  check(!r.overflowX, `${label}: no horizontal page overflow${r.overflowX ? ` -- ${r.spill.slice(0, 3).join('; ') || 'no single element names it'}` : ''}`)
  check(r.escapes.length === 0, `${label}: nothing escapes its clipping ancestor or the viewport${r.escapes.length ? ` -- ${r.escapes.slice(0, 3).join('; ')}` : ''}`)
  check(r.clipped.length === 0, `${label}: no clipped text${r.clipped.length ? ` -- ${r.clipped.slice(0, 3).join('; ')}` : ''}`)
  check(r.overlaps.length === 0, `${label}: no overlapping interactive elements${r.overlaps.length ? ` -- ${r.overlaps.slice(0, 3).join('; ')}` : ''}`)
  check(r.small.length === 0, `${label}: every interactive target ≥ 24×24${r.small.length ? ` -- ${r.small.slice(0, 3).join('; ')}` : ''}`)
  if (r.tiny.length) note(`${label}: ${r.tiny.length} target(s) between 24 and 44px: ${r.tiny.slice(0, 4).join('; ')}`)
  check(r.contrast.length === 0, `${label}: computed text contrast meets the floor${r.contrast.length ? ` -- ${r.contrast.slice(0, 3).join('; ')}` : ''}`)
  check(anim === 0, `${label}: under prefers-reduced-motion nothing animates longer than 1 ms (${anim})`)
  check(errors.length === 0, `${label}: no console error`)
}
note(`${cells} theme × width × stress cells; screenshots under .omp/tests/ui/screens/`)
done()
