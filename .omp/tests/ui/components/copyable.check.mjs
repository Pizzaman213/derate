// requires: browser
// Copyable: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Copyable.
//
//   cd ui && node ../.omp/tests/ui/components/copyable.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Copyable', {
  keyboard: async ({ page, check, g }) => {
    // The copy button reports into itself. Over loopback HTTP there is no
    // secure context, so this is the execCommand path -- the one production runs.
    await g.browser.contexts()[0].grantPermissions(['clipboard-read', 'clipboard-write']).catch(() => {})
    const btn = await page.$('[data-gallery][data-state="copied"] button')
    // The generic Enter/Space pass already fired it once; let it revert (2 s).
    await page.waitForTimeout(2300)
    const before = await btn.textContent()
    await btn.click()
    await page.waitForTimeout(100)
    const after = await btn.textContent()
    check(after !== before && /copied|failed/i.test(after), `the button reports the outcome into itself ("${before}" -> "${after}")`)
    await page.waitForTimeout(2500)
    check((await btn.textContent()) === before, 'and reverts to its label afterwards')
  },
})
