// requires: browser
// OverrideGate: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to OverrideGate.
//
//   cd ui && node ../.omp/tests/ui/components/overridegate.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('OverrideGate', {
  keyboard: async ({ page, check }) => {
    const unchecked = await page.$$('[data-gallery][data-state="unchecked"] button')
    check(unchecked.length === 0, 'no launch button until the claim is ticked')
    const checked = await page.$$('[data-gallery][data-state="checked"] button')
    check(checked.length === 1, 'exactly one launch button once it is ticked')
    const launching = await page.$eval('[data-gallery][data-state="launching"] button', (b) => ({ disabled: b.disabled, text: b.textContent }))
    check(launching.disabled, `the button is disabled while launching ("${launching.text}")`)
    const stacked = await page.$$('[data-gallery][data-state="stacked"] button')
    check(stacked.length === 0, 'stacked gates render no button of their own (the caller puts one below the stack)')
    const label = await page.$eval('[data-gallery][data-state="unchecked"] label', (l) => l.textContent)
    check(/GiB|spark-01/.test(label), 'the checkbox label names the measurement being waived')
  },
})
