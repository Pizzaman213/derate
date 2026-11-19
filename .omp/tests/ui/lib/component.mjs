// The checks every registered component gets, run by its
// tests/ui/components/<name>.check.mjs:
//
//   (a) every variant × state in states.tsx renders: the section is present,
//       has a non-zero box, and the page logged no console error;
//   (b) keyboard: Tab order equals DOM order of the focusables inside the
//       component's sections; Enter and Space on each <button> fire its
//       handler (the gallery counts clicks per section in data-clicks);
//       plus the per-kind contracts passed in `keyboard` (Escape closes a
//       Popover, the Sheet traps Tab both ways and hands focus back, …);
//   (c) axe-core on each section: zero violations at wcag2a / wcag2aa /
//       wcag22aa.
//
//   import { checkComponent } from '../lib/component.mjs'
//   await checkComponent('Readout')
//
// Everything runs against the gallery over loopback HTTP; see browser.mjs.

import { report } from '../../../../ui/src/check/harness.mjs'
import { openGallery } from './browser.mjs'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const registry = JSON.parse(readFileSync(join(here, '..', '..', '..', 'design', 'components', 'registry.json'), 'utf8'))

const FOCUSABLE = 'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'

export async function checkComponent(name, { keyboard = null, query = '' } = {}) {
  const { check, note, done } = report()
  const entry = registry.components.find((c) => c.name === name)
  check(!!entry, `${name} is in components/registry.json`)
  const g = await openGallery({ query: `only=${encodeURIComponent(name)}${query ? '&' + query : ''}` })
  const { page } = g
  try {
    // (a) renders
    const secs = await page.$$eval('[data-gallery]', (els) => els.map((e) => ({ variant: e.dataset.variant, state: e.dataset.state, w: e.getBoundingClientRect().width, h: e.getBoundingClientRect().height, bodyH: e.querySelector('[data-gallery-body]')?.getBoundingClientRect().height ?? 0 })))
    check(secs.length > 0, `${name}: gallery mounts ${secs.length} state(s)`)
    for (const st of entry?.states ?? []) check(secs.some((s) => s.state === st), `${name}: registry state "${st}" has a gallery entry`)
    for (const s of secs) check(s.w > 0 && s.h > 0, `${name} ${s.variant}/${s.state} renders with a box (${Math.round(s.w)}×${Math.round(s.h)})`)
    check(g.errors.length === 0, `${name}: no console error or uncaught exception while mounting${g.errors.length ? ` -- ${g.errors[0].slice(0, 160)}` : ''}`)

    // (b) keyboard: tab order == DOM order. A named radio group is one tab
    // stop (the checked radio, else the first), as browsers implement it.
    const focusables = await page.$$eval(`[data-gallery] ${FOCUSABLE}`, (els) => els.filter((e) => {
      if (e.getBoundingClientRect().width <= 0 || e.tabIndex < 0) return false
      if (e.type !== 'radio' || !e.name) return true
      const group = [...document.querySelectorAll('input[type="radio"]')].filter((r) => r.name === e.name && r.form === e.form && !r.disabled)
      const stop = group.find((r) => r.checked) ?? group[0]
      return stop === e
    }).map((e, i) => { e.setAttribute('data-focus-order', String(i)); return i }))
    if (focusables.length) {
      await page.evaluate(() => { document.body.focus(); window.scrollTo(0, 0) })
      const visited = []
      for (let i = 0; i < Math.min(focusables.length, 60); i++) {
        await page.keyboard.press('Tab')
        const o = await page.evaluate(() => document.activeElement?.getAttribute('data-focus-order'))
        if (o === null || o === undefined) break
        visited.push(Number(o))
      }
      const expected = visited.map((_, i) => i)
      check(visited.length > 0 && visited.every((v, i) => v === expected[i]), `${name}: Tab order follows DOM order (${visited.slice(0, 12).join(',')}${visited.length > 12 ? '…' : ''})`)
      // Enter and Space on each button fire its handler. Looked up by index
      // before every press: a handler may re-render its own button (the chart
      // pack's legend replaces its items on toggle), detaching the old handle.
      const BUTTON = '[data-gallery] button:not([disabled])'
      const nButtons = Math.min((await page.$$(BUTTON)).length, 12)
      let fired = 0, tried = 0
      for (let bi = 0; bi < nButtons; bi++) {
        for (const key of ['Enter', 'Space']) {
          const b = (await page.$$(BUTTON))[bi]
          if (!b) break
          const sec = await b.evaluateHandle((el) => el.closest('[data-gallery]'))
          const before = Number(await sec.evaluate((s) => s.dataset.clicks))
          await b.focus()
          await page.keyboard.press(key)
          await page.waitForTimeout(30)
          const after = Number(await sec.evaluate((s) => s.dataset.clicks))
          tried++
          if (after > before) fired++
        }
      }
      if (tried) check(fired === tried, `${name}: Enter and Space fire every button's handler (${fired}/${tried})`)
    } else note(`${name}: no focusable elements; keyboard checks n/a`)
    if (keyboard) await keyboard({ page, check, note, g })

    // (c) axe per section
    const count = await page.$$eval('[data-gallery]', (els) => els.map((e, i) => { e.setAttribute('data-axe-index', String(i)); return i }).length)
    let worst = null
    for (let i = 0; i < count; i++) {
      const r = await g.axe(`[data-axe-index="${i}"]`)
      if (r.violations.length && !worst) worst = { i, v: r.violations }
      check(r.violations.length === 0, `${name} ${secs[i]?.variant}/${secs[i]?.state}: axe wcag2a/aa/22aa clean${r.violations.length ? ` -- ${r.violations.map((v) => `${v.id}: ${v.nodes[0]?.failureSummary?.split('\n')[1] ?? v.help}`).join('; ').slice(0, 200)}` : ''}`)
    }
  } finally {
    await g.close()
  }
  done()
}
