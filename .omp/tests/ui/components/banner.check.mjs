// requires: browser
// Banner: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Banner.
//
//   cd ui && node ../.omp/tests/ui/components/banner.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Banner', {
  keyboard: async ({ page, check }) => {
    // both tones render, each with its own live-region role
    const roles = await page.$$eval('[data-gallery]', (els) =>
      els.map((e) => ({ variant: e.dataset.variant, role: e.querySelector('[data-tone]')?.getAttribute('role') ?? null })),
    )
    const fault = roles.filter((r) => r.variant === 'fault')
    const warn = roles.filter((r) => r.variant === 'warn')
    check(fault.length > 0 && warn.length > 0, 'both tones render')
    check(fault.every((r) => r.role === 'alert'), 'the fault tone is role="alert"')
    check(warn.every((r) => r.role === 'status'), 'the warn tone is role="status"')
    // the action is a real link, so it can be opened in a second tab
    const href = await page.$eval('[data-gallery][data-state="with-action"] a', (a) => a.getAttribute('href')).catch(() => null)
    check(!!href, `the action renders as a link with an href (${href})`)
    // no dismiss control: a banner reflects state and leaves when it clears
    const buttons = await page.$$eval('[data-gallery] [data-tone] button', (els) => els.length)
    check(buttons === 0, 'no dismiss control')
  },
})
