// requires: browser
// PasswordField: every variant × state renders, keyboard contract, axe clean.
// This file adds the toggle: pressing it flips aria-pressed and the input's
// type, and the value survives the swap.
//
//   cd ui && node ../.omp/tests/ui/components/passwordfield.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('PasswordField', {
  keyboard: async ({ page, check }) => {
    const sel = '[data-gallery][data-state="shown"]'
    const read = () => page.$eval(sel, (el) => ({
      pressed: el.querySelector('button').getAttribute('aria-pressed'),
      type: el.querySelector('input').type,
      value: el.querySelector('input').value,
    }))
    const before = await read()
    check((before.pressed === 'true') === (before.type === 'text'), `aria-pressed matches the input type before (${before.pressed}, ${before.type})`)
    await page.focus(`${sel} button`)
    await page.keyboard.press('Enter')
    await page.waitForTimeout(30)
    const after = await read()
    check(after.pressed !== before.pressed, `Enter on the toggle flips aria-pressed (${before.pressed} -> ${after.pressed})`)
    check(after.type !== before.type && ['text', 'password'].includes(after.type), `and the input type (${before.type} -> ${after.type})`)
    check(after.value === before.value, 'the value survives the type swap')
    await page.keyboard.press('Space')
    await page.waitForTimeout(30)
    const back = await read()
    check(back.pressed === before.pressed && back.type === before.type, 'Space flips it back')
  },
})
