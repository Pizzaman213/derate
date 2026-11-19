// requires: browser
// Button: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Button.
//
//   cd ui && node ../.omp/tests/ui/components/button.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Button', {
  keyboard: async ({ page, check }) => {
    // a disabled button is skipped by Tab and never fires
    const disabled = await page.$$eval('[data-gallery][data-state="disabled"] button', (els) => els.map((e) => e.disabled))
    check(disabled.length > 0 && disabled.every(Boolean), 'disabled buttons carry the disabled attribute (not just a class)')
    const pressed = await page.$eval('[data-gallery][data-state="pressed"] button[aria-pressed="true"]', (e) => getComputedStyle(e).backgroundColor)
    const unpressed = await page.$eval('[data-gallery][data-state="pressed"] button[aria-pressed="false"]', (e) => getComputedStyle(e).backgroundColor)
    check(pressed !== unpressed, `a pressed chip is painted differently from an unpressed one (${pressed} vs ${unpressed})`)
    const iconOnly = await page.$$eval('[data-gallery][data-state="icon-only"] button', (els) => els.map((e) => e.getAttribute('aria-label')))
    check(iconOnly.every((l) => l && l.length > 0), 'icon-only buttons carry aria-label')
  },
})
