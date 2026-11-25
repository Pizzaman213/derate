// requires: browser coordinator -- counts what the real UI asks the coordinator
//
// The UI's own polling, measured from the network rather than inferred from the
// hooks. Before the shared store one browser on the dashboard sent bursts of
// sixteen identical /api/cluster reads a second, ~200 a minute, and every hidden
// destination and sub-pane polled exactly as hard as the one on screen -- which
// is what buried the coordinator's access log. Four things are pinned here:
// one read per interval for a shared name, nothing from a hidden pane, nothing
// from a background browser tab, and a pane that fetches the moment it is shown.
//
//   DERATE_CHECK_ORIGIN=http://127.0.0.1:18170 DERATE_CHECK_ADMIN_USER=... \
//     DERATE_CHECK_ADMIN_PASSWORD=... node src/state/polling.check.mjs

import { ORIGIN, sessionCookie } from '../check/auth.mjs'
import { chromium } from 'playwright-core'
import { findBrowser } from '../check/browser.mjs'
import { report } from '../check/harness.mjs'

const { check, note, done } = report()

const browser = findBrowser()
if (!browser.path) {
  console.error(`no browser: ${browser.why}`)
  process.exit(1)
}
if (!sessionCookie) {
  console.error('no admin session: set DERATE_CHECK_ADMIN_USER / DERATE_CHECK_ADMIN_PASSWORD')
  process.exit(1)
}

let seen = []
const count = (test) => seen.filter(test).length
const path = (p) => (u) => u.pathname === p
// Overview's by-provider spend read: the dashboard's own usage card never
// groups by provider alone, so any of these comes from the hidden Usage pane.
const spendByProvider = (u) => u.pathname === '/api/usage' && u.searchParams.get('group_by') === 'provider'

/** Wait up to `ms` for `test` to match a request seen since the last reset. */
const within = async (page, ms, test) => {
  const end = Date.now() + ms
  while (Date.now() < end) {
    if (count(test) > 0) return true
    await page.waitForTimeout(100)
  }
  return count(test) > 0
}

const setVisibility = (page, state) =>
  page.evaluate((s) => {
    Object.defineProperty(document, 'visibilityState', { value: s, configurable: true })
    Object.defineProperty(document, 'hidden', { value: s === 'hidden', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
  }, state)

const engine = await chromium.launch({ executablePath: browser.path })
const page = await engine.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
await page.context().addCookies([sessionCookie])
page.on('request', (r) => {
  const u = new URL(r.url())
  if (u.origin === new URL(ORIGIN).origin) seen.push(u)
})

try {
  // 1. The dashboard for 20 s: shared names once per interval, hidden panes silent.
  await page.goto(`${ORIGIN}/dashboard`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(20000)
  const cluster = count(path('/api/cluster'))
  const deployments = count(path('/api/deployments'))
  note(`20 s on /dashboard: ${seen.filter((u) => u.pathname.startsWith('/api/')).length} /api requests`)
  check(cluster >= 1 && cluster <= 6, `/api/cluster read once per 5 s interval, not per component (${cluster} in 20 s)`)
  check(deployments <= 6, `/api/deployments likewise (${deployments} in 20 s)`)
  check(count(path('/api/requests/origins')) === 0, 'the hidden Usage → Requests pane reads no origins')
  check(count(spendByProvider) === 0, 'the hidden Usage → Overview pane reads no spend')
  check(count(path('/api/audit')) === 0, 'the hidden Settings → Audit pane reads no audit log')

  // 2. A background browser tab polls nothing.
  await setVisibility(page, 'hidden')
  seen = []
  await page.waitForTimeout(12000)
  const hidden = count(path('/api/cluster'))
  check(hidden === 0, `a hidden browser tab reads /api/cluster ${hidden} times in 12 s`)
  note(`while hidden: ${seen.filter((u) => u.pathname.startsWith('/api/')).map((u) => u.pathname).join(', ') || 'no /api request'}`)

  // 3. Coming back fetches what went stale, at once.
  seen = []
  await setVisibility(page, 'visible')
  check(await within(page, 2000, path('/api/cluster')), 'showing the tab again reads /api/cluster within 2 s')

  // 4. A pane shown for the first time fetches at once.
  seen = []
  await page.click('nav.dest a[href="/usage"]')
  await page.click('#us-tab-requests')
  check(await within(page, 3000, path('/api/requests/origins')), 'opening Usage → Requests reads its origins within 3 s')
} finally {
  await engine.close()
}

done()
