// requires: browser
// Popover: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Popover.
//
//   cd ui && node ../.omp/tests/ui/components/popover.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Popover', {
  keyboard: async ({ page, check }) => {
    const trigger = await page.$('[data-gallery][data-state="open"] button[aria-haspopup]')
    check(!!trigger, 'the trigger is a button with aria-haspopup')
    await trigger.click(); await page.waitForTimeout(50)
    check((await trigger.getAttribute('aria-expanded')) === 'true', 'clicking opens it (aria-expanded=true)')
    const panelId = await trigger.getAttribute('aria-controls')
    check(!!panelId && (await page.evaluate((id) => !!document.getElementById(id), panelId)), 'aria-controls names the panel that is in the DOM')
    await page.keyboard.press('Escape'); await page.waitForTimeout(50)
    check((await trigger.getAttribute('aria-expanded')) === 'false', 'Escape closes it')
    const focused = await page.evaluate(() => document.activeElement?.getAttribute('aria-haspopup'))
    check(focused === 'true', 'and focus is back on the trigger')
    await trigger.click(); await page.waitForTimeout(50)
    const done = await page.$('[data-gallery][data-state="open"] button:has-text("Done")')
    await done.click(); await page.waitForTimeout(50)
    check((await trigger.getAttribute('aria-expanded')) === 'false', 'a child calling close() closes it')
  },
})
