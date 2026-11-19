// requires: browser
// HostTable: every variant × state renders, keyboard contract, axe clean,
// plus what the table promises: which radio is checked, the cheapest chip,
// the fallback box's enablement and the empty sentence.
//
//   cd ui && node ../.omp/tests/ui/components/hosttable.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('HostTable', {
  keyboard: async ({ page, check }) => {
    const at = (state) => `[data-gallery][data-state="${state}"]`
    const checked = (state) =>
      page.$eval(at(state), (el) => el.querySelector('input[type="radio"]:checked')?.closest('tr')?.dataset.host ?? null)
    check((await checked('default')) === '', 'no pin: the Automatic row is checked')
    check((await checked('pinned')) === 'google-vertex', 'the pinned row is checked')
    const cheap = await page.$$eval(`${at('default')} tr`, (rows) =>
      rows.filter((r) => [...r.querySelectorAll('.pill')].some((p) => p.textContent === 'cheapest')).map((r) => r.dataset.host))
    check(cheap.length === 1 && cheap[0] === 'anthropic', `cheapest chip on the min-output row only (${cheap})`)
    const box = (state) => page.$eval(at(state), (el) => el.querySelector('input[type="checkbox"]').disabled)
    check((await box('default')) === true, 'fallback disabled with no pin')
    check((await box('pinned')) === false, 'fallback enabled with a pin')
    const empty = await page.$eval(at('empty'), (el) => el.textContent)
    check(empty.includes('OpenRouter lists no hosts for this model yet.'), 'empty sentence')
    const err = await page.$eval(at('error'), (el) => el.textContent)
    check(err.includes("openrouter lists no backend 'nope'"), 'error sentence verbatim')
    const labels = await page.$$eval(`${at('default')} input[type="radio"]`, (els) => els.map((e) => e.getAttribute('aria-label')))
    check(labels.includes('Use Google Vertex'), 'radios are labelled Use <host>')
  },
})
