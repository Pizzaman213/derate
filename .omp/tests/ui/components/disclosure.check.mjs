// requires: browser
// Disclosure / Section: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Disclosure / Section.
//
//   cd ui && node ../.omp/tests/ui/components/disclosure.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Section')
await checkComponent('Disclosure', {
  keyboard: async ({ page, check }) => {
    const btn = await page.$('[data-gallery][data-state="closed"] button[aria-expanded]')
    check(!!btn, 'the summary is a button with aria-expanded')
    const expanded = async () => btn.getAttribute('aria-expanded')
    check((await expanded()) === 'false', 'closed state reports aria-expanded=false')
    await btn.focus(); await page.keyboard.press('Enter'); await page.waitForTimeout(30)
    check((await expanded()) === 'true', 'Enter opens it')
    await page.keyboard.press('Space'); await page.waitForTimeout(30)
    check((await expanded()) === 'false', 'Space closes it')
    const icon = await page.$eval('[data-gallery][data-state="open"] button svg', (s) => s.dataset.icon)
    check(icon === 'minus', `an open disclosure shows the minus icon (${icon})`)
  },
})
