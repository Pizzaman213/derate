// requires: browser
// Sheet: the one modal. Opens for each kind against the recorded fixtures,
// traps Tab in both directions, closes on Escape, hands focus back to the
// opener, and is axe-clean. Provider-bound, so it is driven by
// ?only=Sheet&variant=<kind> rather than mounted as a plain state.
//
//   cd ui && node ../.omp/tests/ui/components/sheet.check.mjs
import { report } from '../../../../ui/src/check/harness.mjs'
import { openGallery } from '../lib/browser.mjs'

const { check, note, done } = report()
for (const kind of ['node', 'dep', 'model']) {
  const g = await openGallery({ query: `only=Sheet&variant=${kind}` })
  const { page } = g
  try {
    await page.waitForSelector('.sheet.on [role="dialog"], .sheet.on', { timeout: 10000 })
    await page.waitForTimeout(800)
    const dialog = await page.$('[role="dialog"]')
    check(!!dialog, `${kind}: the sheet renders role="dialog"`)
    check((await dialog?.getAttribute('aria-modal')) === 'true', `${kind}: aria-modal="true"`)
    const labelled = await dialog?.evaluate((d) => !!(d.getAttribute('aria-labelledby') && document.getElementById(d.getAttribute('aria-labelledby'))) || !!d.getAttribute('aria-label'))
    check(labelled, `${kind}: the dialog is labelled`)
    check(g.errors.length === 0, `${kind}: no console error while opening${g.errors.length ? ' -- ' + g.errors[0].slice(0, 160) : ''}`)
    // initial focus is inside
    check(await page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]')), `${kind}: focus moves into the dialog`)
    // Tab from the last focusable wraps to the first; Shift+Tab from the first wraps to the last
    const n = await page.$$eval('[role="dialog"] a[href],[role="dialog"] button:not([disabled]),[role="dialog"] input:not([disabled]),[role="dialog"] select:not([disabled]),[role="dialog"] textarea:not([disabled]),[role="dialog"] [tabindex]:not([tabindex="-1"])', (els) => els.filter((e) => e.getBoundingClientRect().width > 0).map((e, i) => { e.setAttribute('data-trap', String(i)); return i }).length)
    note(`${kind}: ${n} focusable element(s) inside`)
    if (n > 1) {
      await page.focus(`[data-trap="${n - 1}"]`)
      await page.keyboard.press('Tab')
      const wrapped = await page.evaluate(() => document.activeElement?.getAttribute('data-trap'))
      check(wrapped === '0', `${kind}: Tab from the last control wraps to the first (landed on ${wrapped})`)
      await page.focus('[data-trap="0"]')
      await page.keyboard.press('Shift+Tab')
      const back = await page.evaluate(() => document.activeElement?.getAttribute('data-trap'))
      check(back === String(n - 1), `${kind}: Shift+Tab from the first wraps to the last (landed on ${back})`)
    }
    const r = await g.axe('.sheet.on')
    check(r.violations.length === 0, `${kind}: axe clean${r.violations.length ? ' -- ' + r.violations.map((v) => v.id).join(', ') : ''}`)
    await page.keyboard.press('Escape')
    await page.waitForTimeout(100)
    check(!(await page.$('[role="dialog"]')), `${kind}: Escape closes it`)
    const restored = await page.evaluate(() => document.activeElement === document.body || !document.activeElement?.closest('.sheet'))
    check(restored, `${kind}: focus is handed back out of the sheet`)
  } finally {
    await g.close()
  }
}
done()
