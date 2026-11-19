// requires: browser coordinator -- mints a real key through the real screen
//
// The one-time reveal, end to end, in a real browser against a live
// coordinator. A `drt_` secret is shown exactly once (RevealModal, from the
// `secret_once` of POST /api/keys) and must then be gone everywhere a person
// could look again: the page after "I saved it", the page after a reload, and
// the key's own GET /api/keys/{id}. The server keeps only a hash, so a secret
// that shows up again anywhere is a secret something kept.
//
//   DERATE_CHECK_ORIGIN=http://localhost:18120 DERATE_CHECK_ADMIN_USER=... \
//     DERATE_CHECK_ADMIN_PASSWORD=... node src/tabs/keys/reveal.check.mjs

// First, for its side effect: logs in, and authenticates every fetch below.
import { ORIGIN, sessionCookie } from '../../check/auth.mjs'
import { chromium } from 'playwright-core'
import { findBrowser } from '../../check/browser.mjs'
import { report } from '../../check/harness.mjs'

const { check, note, done } = report()
const DRT_KEY = /\bdrt_[a-z0-9]{10}_[A-Za-z0-9_-]{32,}/

const browser = findBrowser()
if (!browser.path) {
  console.error(`no browser: ${browser.why}`)
  process.exit(1)
}
if (!sessionCookie) {
  console.error('no admin session: set DERATE_CHECK_ADMIN_USER / DERATE_CHECK_ADMIN_PASSWORD')
  process.exit(1)
}

const engine = await chromium.launch({ executablePath: browser.path })
const page = await engine.newPage({
  viewport: { width: 1440, height: 900 },
  reducedMotion: 'reduce',
})
await page.context().addCookies([sessionCookie])

// Every response the page receives, so "the secret came back over the wire
// again" is checked as well as "the secret is on screen again".
const bodies = []
page.on('response', async (r) => {
  if (!r.url().startsWith(ORIGIN)) return
  try {
    bodies.push({ url: r.url(), text: await r.text() })
  } catch {
    // A navigation or an aborted fetch has no body to read.
  }
})

try {
  const name = `reveal-check-${Date.now()}`
  await page.goto(`${ORIGIN}/usage/keys`, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: 'New key' }).click()
  await page.getByRole('dialog', { name: 'New API key' }).waitFor({ timeout: 5000 })
  await page.locator('#kc-name').fill(name)
  await page.getByRole('button', { name: 'Create key' }).click()

  // The form dialog gives way to the reveal, which is labelled by the key id.
  const dialog = page.getByRole('dialog', { name: /^API key / })
  await dialog.waitFor({ timeout: 15000 })
  const shown = await dialog.innerText()
  const secret = shown.match(DRT_KEY)?.[0] ?? null
  check(secret !== null, 'the new key is revealed in the dialog')
  const all = (await page.evaluate(() => document.body.innerText)).match(new RegExp(DRT_KEY.source, 'g')) ?? []
  check(all.length === 1, `the secret appears on the page exactly once (${all.length})`)
  const keyId = secret ? secret.slice(4, 14) : null

  // The one response allowed to carry it: POST /api/keys.
  const carriers = bodies.filter((b) => secret && b.text.includes(secret)).map((b) => new URL(b.url).pathname)
  check(
    carriers.length === 1 && carriers[0] === '/api/keys',
    `only POST /api/keys carried the secret (${carriers.join(', ') || 'none'})`,
  )

  await page.getByRole('button', { name: 'I saved it' }).click()
  await page.waitForLoadState('networkidle').catch(() => {})
  const after = await page.evaluate(() => document.body.innerHTML)
  check(!!secret && !after.includes(secret), 'the secret is gone from the DOM after "I saved it"')
  check(
    !DRT_KEY.test(await page.evaluate(() => document.body.innerText)),
    'no drt_ key is on screen after "I saved it"',
  )
  if (keyId) check(page.url().includes(`/usage/keys/${keyId}`), `the browser lands on the key's own page (${page.url()})`)

  bodies.length = 0
  await page.reload({ waitUntil: 'networkidle' })
  const reloaded = await page.evaluate(() => document.body.innerHTML)
  check(!!secret && !reloaded.includes(secret), 'the secret is not on the page after a reload')
  const leaked = bodies.filter((b) => secret && b.text.includes(secret)).map((b) => b.url)
  check(
    leaked.length === 0,
    `no response after the reload carried the secret${leaked.length ? ` -- ${leaked[0]}` : ''}`,
  )

  if (keyId) {
    const row = await (await fetch(`${ORIGIN}/api/keys/${keyId}`)).text()
    check(!row.includes(secret), 'GET /api/keys/{id} does not return the secret')
    check(!DRT_KEY.test(row), 'GET /api/keys/{id} contains no drt_ key at all')
    // Clean up: the key exists only for this check.
    const del = await fetch(`${ORIGIN}/api/keys/${keyId}`, {
      method: 'DELETE',
      headers: { origin: ORIGIN },
    })
    note(`deleted ${keyId}: ${del.status}`)
  }
} finally {
  await engine.close()
}
done()
