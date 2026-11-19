// requires: browser coordinator -- opens the balancer dialog on the real /cluster screen
//
// The load balancer dialog, in a real browser against a live coordinator:
// double-clicking a split opens it, its policy select offers auto plus the
// seven policies, a limit set under auto says the policy will be pinned, and
// "Remove this load balancer" asks exactly the sentence doc.ts writes --
// checked against the template too, so the two cannot drift together.
// window.confirm is stubbed to answer no: this check changes nothing.
//
//   DERATE_CHECK_ORIGIN=http://localhost:18412 DERATE_CHECK_ADMIN_USER=... \
//     DERATE_CHECK_ADMIN_PASSWORD=... node src/tabs/cluster/dialog.check.mjs

import { ORIGIN, sessionCookie } from '../../check/auth.mjs'
import { chromium } from 'playwright-core'
import { findBrowser } from '../../check/browser.mjs'
import { load, report } from '../../check/harness.mjs'

const D = await load(import.meta.url, './doc.ts')
const { check, done } = report()

const browser = findBrowser()
if (!browser.path) {
  console.error(`no browser: ${browser.why}`)
  process.exit(1)
}
if (!sessionCookie) {
  console.error('no admin session: set DERATE_CHECK_ADMIN_USER / DERATE_CHECK_ADMIN_PASSWORD')
  process.exit(1)
}

const get = async (p) => (await fetch(`${ORIGIN}${p}`)).json()
const [routing, providers, deployments] = await Promise.all([get('/api/routing'), get('/api/providers'), get('/api/deployments')])
const snapshot = {
  topology: { nodes: [], edges: [] },
  cluster: { deployments },
  routing,
  providers,
  catalogues: {},
  mtp: {},
  spec: {},
}
// A name with a local deployment says the most; any served name will do.
const served =
  routing.find((r) => deployments.some((d) => d.served_name === r.served_name))?.served_name ?? routing[0]?.served_name
if (!served) {
  console.error('the coordinator routes nothing: no load balancer to open')
  process.exit(1)
}

const engine = await chromium.launch({ executablePath: browser.path })
const page = await engine.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
await page.context().addCookies([sessionCookie])

try {
  await page.goto(`${ORIGIN}/cluster`, { waitUntil: 'networkidle' })
  const split = page.locator(`.react-flow__node[data-id="split:${served}"] .wtri-text`)
  await split.waitFor({ timeout: 15000 })
  await split.dblclick()
  const dialog = page.getByRole('dialog', { name: `${served} load balancer` })
  await dialog.waitFor({ timeout: 5000 })
  check(true, `double-clicking ${served}'s split opens its dialog`)

  const select = dialog.getByRole('combobox', { name: 'Routing policy' })
  const options = await select.locator('option').allTextContents()
  check(options.length === 8, `the policy select offers auto and the seven policies (${options.length})`)
  check(/^auto — what the router picks \(.+\)$/.test(options[0] ?? ''), `auto comes first and names what it resolves to (${options[0]})`)

  if ((await select.inputValue()) !== 'auto') await select.selectOption('auto')
  await dialog.getByLabel('rpm').fill('3')
  const note = dialog.getByText(D.BALANCER_SENTENCES.limitPins, { exact: true })
  check(await note.isVisible(), 'a limit under auto says it pins the policy')
  await select.selectOption('round_robin')
  check(!(await note.isVisible()), 'and the note goes once a policy is picked')

  // Shares: with two or more targets, dragging one slider moves the others,
  // and the split goes to Pending changes as a route.set under weighted_capacity.
  const sliders = dialog.getByRole('slider')
  if ((await sliders.count()) >= 2) {
    await sliders.first().fill('80')
    const values = (await sliders.evaluateAll((xs) => xs.map((x) => Number(x.value))))
    check(values[0] === 80 && values.reduce((a, b) => a + b, 0) === 100, `one slider at 80 leaves the rest summing to 20 (${values})`)
    check(await dialog.getByText(D.BALANCER_SENTENCES.manualSplit).isVisible(), 'the dialog says the split is set by hand')
    await page.waitForTimeout(200)
    const pending = (await page.locator('.wops').textContent()) ?? ''
    check(/"policy": "weighted_capacity"/.test(pending) && /"shares": \{/.test(pending), 'Pending changes carries the split as a route.set')
  }

  const removable = dialog.getByRole('button', { name: 'Remove this load balancer' })
  if (await removable.count()) {
    await page.evaluate(() => {
      window.__asked = []
      window.confirm = (m) => {
        window.__asked.push(m)
        return false
      }
    })
    await removable.click()
    const asked = await page.evaluate(() => window.__asked[0] ?? null)
    const expected = D.removalSentence(D.fromServer(snapshot), snapshot, served)
    check(asked === expected, 'the confirm is the sentence doc.ts writes')
    if (asked !== expected) console.log('      asked', JSON.stringify(asked), '\n   expected', JSON.stringify(expected))
    check(
      new RegExp(
        `^Remove ${served.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\?\\n\\nStops: .+\\nSwitches off: .+\\nClears: its routing override and limits\\n\\nNothing happens until you Save\\.$`,
      ).test(asked ?? ''),
      'and it follows the template line for line',
    )
    check(await dialog.isVisible(), 'answering no leaves the dialog open and nothing removed')
  }

  await page.keyboard.press('Escape')
  await dialog.waitFor({ state: 'detached', timeout: 3000 })
  check(true, 'Escape closes it')
} finally {
  await engine.close()
}

done()
