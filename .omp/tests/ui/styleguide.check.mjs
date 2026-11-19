// requires: browser
// The living style guide opens from file://, switches themes without a
// console error, and is captured in each theme to design/screens/.
//
//   cd ui && node ../.omp/tests/ui/styleguide.check.mjs

import { mkdirSync, existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { report } from '../../../ui/src/check/harness.mjs'
import { launch } from './lib/browser.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const INDEX = resolve(here, '..', '..', 'design', 'index.html')
const SCREENS = resolve(here, '..', '..', 'design', 'screens')
const { check, note, done } = report()

check(existsSync(INDEX), 'design/index.html exists (else: cd .omp && npm run styleguide:build)')
const browser = await launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 1600 } })
const errors = []
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
await page.goto(pathToFileURL(INDEX).href)
check((await page.$$('.sw')).length > 40, `swatches rendered (${(await page.$$('.sw')).length})`)
check((await page.$$('.comp')).length >= 20, `component sections rendered (${(await page.$$('.comp')).length})`)
check((await page.$$('.ic')).length >= 41, `icons rendered (${(await page.$$('.ic')).length})`)
mkdirSync(SCREENS, { recursive: true })
for (const theme of ['light', 'dark', 'high-contrast']) {
  await page.click(`[data-theme-set="${theme}"]`)
  await page.waitForTimeout(100)
  check((await page.getAttribute('html', 'data-theme')) === theme, `${theme}: the theme button sets data-theme`)
  const panel = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
  note(`${theme}: body background ${panel}`)
  await page.screenshot({ path: join(SCREENS, `styleguide-${theme}.png`), fullPage: false })
  await page.evaluate(() => document.getElementById('c-readout')?.scrollIntoView())
  await page.screenshot({ path: join(SCREENS, `styleguide-${theme}-components.png`), fullPage: false })
  await page.evaluate(() => window.scrollTo(0, 0))
}
await page.check('#sbs')
check(await page.evaluate(() => document.body.classList.contains('side-by-side')), 'side-by-side toggle works')
check(errors.length === 0, `no console error or uncaught exception${errors.length ? ' -- ' + errors[0].slice(0, 160) : ''}`)
await browser.close()
note(`screens: ${SCREENS}/styleguide-{light,dark,high-contrast}.png`)
done()
