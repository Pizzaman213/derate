// requires: browser
// PackChart: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to PackChart.
//
//   cd ui && node ../.omp/tests/ui/components/packchart.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('PackChart', {
  keyboard: async ({ page, check }) => {
    // the pack is lazy-loaded: wait for it to mount before asserting
    await page.waitForSelector('[data-gallery] .oc-root')
    const sections = await page.$$eval('[data-gallery]', (els) =>
      els.map((e) => ({
        state: e.dataset.state,
        variant: e.dataset.variant,
        svg: !!e.querySelector('.oc-root svg'),
        text: e.textContent ?? '',
      })),
    )
    const defaults = sections.filter((s) => s.state === 'default')
    for (const s of defaults) check(s.svg, `${s.variant} default draws an svg`)
    const empty = sections.find((s) => s.state === 'empty')
    check(!!empty && empty.text.includes('No requests in this window.'), 'the empty state shows its emptyMessage')
  },
})
