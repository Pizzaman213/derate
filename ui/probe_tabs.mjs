import { chromium } from 'playwright-core'
import { findBrowser } from './src/check/browser.mjs'

const browser = findBrowser()
if (!browser.path) {
  console.error('no chromium:', browser.why)
  process.exit(1)
}

const base = process.argv[2] || 'http://localhost:8088'
const tabs = ['dash', 'models', 'cluster', 'chat', 'spend', 'settings']

const engine = await chromium.launch({ executablePath: browser.path })
const page = await engine.newPage()

page.on('console', (msg) => {
  if (msg.type() === 'error') console.log(`[console.error] ${msg.text()}`)
})
page.on('pageerror', (err) => console.log(`[pageerror] ${err.message}\n${err.stack}`))
page.on('requestfailed', (req) => console.log(`[requestfailed] ${req.url()} ${req.failure()?.errorText}`))
page.on('response', (res) => {
  if (res.status() >= 400) console.log(`[http ${res.status()}] ${res.url()}`)
})

await page.goto(`${base}/dash`, { waitUntil: 'load' })
await page.waitForTimeout(800)

for (const tab of tabs) {
  console.log(`\n--- navigating to /${tab} ---`)
  try {
    await page.goto(`${base}/${tab}`, { waitUntil: 'load', timeout: 15000 })
  } catch (e) {
    console.log(`[goto failed] ${e.message}`)
  }
  await page.waitForTimeout(800)
  const rootHtml = await page.evaluate(() => document.getElementById('root')?.innerHTML?.length ?? -1)
  const bodyText = await page.evaluate(() => document.body.innerText.slice(0, 200))
  console.log(`#root innerHTML length: ${rootHtml}`)
  console.log(`body text sample: ${JSON.stringify(bodyText)}`)
}

await engine.close()
