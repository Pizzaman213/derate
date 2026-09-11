import { chromium } from 'playwright-core'
import { findBrowser } from './src/check/browser.mjs'

const browser = findBrowser()
if (!browser.path) {
  console.error('no chromium:', browser.why)
  process.exit(1)
}

const base = process.argv[2] || 'http://localhost:8088'
const engine = await chromium.launch({ executablePath: browser.path })
const context = await engine.newContext()

function wire(page, label) {
  page.on('console', (msg) => {
    if (msg.type() === 'error') console.log(`[${label} console.error] ${msg.text()}`)
  })
  page.on('pageerror', (err) => console.log(`[${label} pageerror] ${err.message}`))
  page.on('crash', () => console.log(`[${label} CRASHED]`))
  page.on('response', (res) => {
    if (res.status() >= 400) console.log(`[${label} http ${res.status()}] ${res.url()}`)
  })
}

console.log('opening tab 1 -> /dash')
const t1 = await context.newPage()
wire(t1, 'tab1')
await t1.goto(`${base}/dash`, { waitUntil: 'load' })
await t1.waitForTimeout(1500)
console.log('tab1 alive, #root length:', await t1.evaluate(() => document.getElementById('root')?.innerHTML?.length))

console.log('\nopening tab 2 -> /dash (second tab, same origin)')
const t2 = await context.newPage()
wire(t2, 'tab2')
await t2.goto(`${base}/dash`, { waitUntil: 'load' })
await t2.waitForTimeout(1500)
console.log('tab2 alive, #root length:', await t2.evaluate(() => document.getElementById('root')?.innerHTML?.length))

console.log('\nchecking tab1 is still alive after tab2 opened...')
await t1.waitForTimeout(1000)
try {
  const len = await t1.evaluate(() => document.getElementById('root')?.innerHTML?.length)
  console.log('tab1 STILL alive, #root length:', len)
} catch (e) {
  console.log('tab1 evaluate FAILED:', e.message)
}

// third tab, and a chat tab too, since chat opens a streaming connection
console.log('\nopening tab 3 -> /chat')
const t3 = await context.newPage()
wire(t3, 'tab3')
await t3.goto(`${base}/chat`, { waitUntil: 'load' })
await t3.waitForTimeout(1500)
console.log('tab3 alive, #root length:', await t3.evaluate(() => document.getElementById('root')?.innerHTML?.length))

for (const [label, p] of [['tab1', t1], ['tab2', t2], ['tab3', t3]]) {
  await p.waitForTimeout(500)
  try {
    const len = await p.evaluate(() => document.getElementById('root')?.innerHTML?.length)
    console.log(`${label} final check, #root length:`, len)
  } catch (e) {
    console.log(`${label} final check FAILED:`, e.message)
  }
}

await engine.close()
