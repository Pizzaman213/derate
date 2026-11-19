// requires: browser
// RevealModal: the one-time drt_ key reveal. A body portal, so like the Sheet
// it is driven by ?only=RevealModal&variant=<state> rather than mounted as a
// plain section. Asserts the dialog semantics, that focus lands in it, that the
// whole secret is on screen and inside the card, that neither Escape nor a
// scrim click dismisses it, that "I saved it" (click, Enter) is the exit and
// takes the secret off the page, and that it is axe-clean.
//
//   cd ui && node ../.omp/tests/ui/components/reveal-modal.check.mjs
import { report } from '../../../../ui/src/check/harness.mjs'
import { openGallery } from '../lib/browser.mjs'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const registry = JSON.parse(readFileSync(join(here, '..', '..', '..', 'design', 'components', 'registry.json'), 'utf8'))
const entry = registry.components.find((c) => c.name === 'RevealModal')
const states = readFileSync(join(here, '..', 'gallery', 'states.tsx'), 'utf8')
const SECRETS = Object.fromEntries([...states.matchAll(/^\s+(\w+): \{ keyId: '(\w+)', secret: '([^']+)' \}/gm)].map((m) => [m[1], { keyId: m[2], secret: m[3] }]))

const { check, note, done } = report()
check(!!entry, 'RevealModal is in components/registry.json')
for (const state of entry?.states ?? []) {
  const fx = SECRETS[state]
  check(!!fx, `${state}: has a REVEAL_FIXTURES entry`)
  if (!fx) continue
  check(/^drt_[a-z0-9]{10}_[A-Za-z0-9_-]{43}$/.test(fx.secret), `${state}: the fixture is the shape the server mints`)
  for (const [action, how] of [['click', 'click'], ['enter', 'Enter']]) {
    const g = await openGallery({ query: `only=RevealModal&variant=${state}` })
    const { page } = g
    try {
      await page.waitForSelector('.sheet.on [role="dialog"]', { timeout: 10000 })
      await page.waitForTimeout(200)
      const tag = `${state}/${action}`
      if (action === 'click') {
        const dialog = await page.$('[role="dialog"]')
        check((await dialog.getAttribute('aria-modal')) === 'true', `${tag}: role="dialog" with aria-modal="true"`)
        const name = await dialog.evaluate((d) => document.getElementById(d.getAttribute('aria-labelledby') ?? '')?.textContent ?? d.getAttribute('aria-label') ?? '')
        check(name.includes(fx.keyId), `${tag}: the dialog's accessible name names the key id ("${name}")`)
        check(await page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]')), `${tag}: focus moves into the dialog on open`)
        const shown = await page.evaluate((s) => document.querySelector('[role="dialog"]').textContent.includes(s), fx.secret)
        check(shown, `${tag}: the whole secret is on screen`)
        const fits = await page.evaluate(() => { const d = document.querySelector('[role="dialog"]').getBoundingClientRect(); return [...document.querySelectorAll('[role="dialog"] *')].every((e) => { const r = e.getBoundingClientRect(); return r.width === 0 || (r.left >= d.left - 1 && r.right <= d.right + 1) }) && d.right <= innerWidth + 1 })
        const clipped = await page.evaluate(() => [...document.querySelectorAll('[role="dialog"] *')].some((e) => e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).overflowX !== 'visible'))
        check(!clipped, `${tag}: the secret wraps instead of scrolling out of sight`)
        check(fits, `${tag}: nothing overflows the card or the viewport`)
        check(g.errors.length === 0, `${tag}: no console error${g.errors.length ? ' -- ' + g.errors[0].slice(0, 160) : ''}`)
        const r = await g.axe('.sheet.on')
        check(r.violations.length === 0, `${tag}: axe clean${r.violations.length ? ' -- ' + r.violations.map((v) => v.id).join(', ') : ''}`)
        await page.keyboard.press('Escape')
        await page.waitForTimeout(100)
        check(!!(await page.$('[role="dialog"]')), `${tag}: Escape does not dismiss it`)
        const box = await page.$eval('.sheet.on', (s) => { const r = s.getBoundingClientRect(); return { x: r.left + 4, y: r.top + 4 } })
        await page.mouse.click(box.x, box.y)
        await page.waitForTimeout(100)
        check(!!(await page.$('[role="dialog"]')), `${tag}: a scrim click does not dismiss it`)
        check(!(await page.evaluate(() => document.body.dataset.revealDone)), `${tag}: onDone has not been called yet`)
        // Tab and Shift+Tab past either end stay in the dialog: focus never
        // reaches the page behind a modal that cannot be dismissed.
        let escaped = false
        for (const key of ['Tab', 'Tab', 'Tab', 'Tab', 'Shift+Tab', 'Shift+Tab', 'Shift+Tab', 'Shift+Tab']) {
          await page.keyboard.press(key)
          if (!(await page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]')))) escaped = true
        }
        check(!escaped, `${tag}: Tab and Shift+Tab stay inside the dialog`)
      }
      const btn = page.getByRole('button', { name: 'I saved it' })
      if (how === 'click') await btn.click()
      else { await btn.focus(); await page.keyboard.press('Enter') }
      await page.waitForTimeout(100)
      check((await page.evaluate(() => document.body.dataset.revealDone)) === '1', `${tag}: "I saved it" calls onDone exactly once`)
      check(!(await page.evaluate((s) => document.body.innerHTML.includes(s), fx.secret)), `${tag}: the secret is gone from the page afterwards`)
    } finally {
      await g.close()
    }
  }
}
