#!/usr/bin/env node
// Open gallery.html from disk (file://, no server, no network), render EVERY card in
// every theme, and fail on any error card, page error or network request. Writes
// screenshots of the top of the page per theme to tests/.build/gallery-<theme>.png.
//
//   node tests/browser/gallery.mjs

import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { ROOT, BUILD } from './lib.mjs'

mkdirSync(BUILD, { recursive: true })
const browser = await chromium.launch({ headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const problems = []
page.on('pageerror', (e) => problems.push(`page error: ${e}`))
page.on('console', (m) => { if (m.type() === 'error') problems.push(`console: ${m.text()}`) })
page.on('request', (r) => { if (!/^(file|data|blob):/.test(r.url())) problems.push(`network request: ${r.url()}`) })
await page.goto(pathToFileURL(join(ROOT, 'gallery.html')).href)
let total = 0
for (const theme of ['all', 'light', 'dark', 'high-contrast']) {
  await page.selectOption('#theme', theme)
  const drawn = await page.evaluate(() => window.__galleryRenderAll())
  const res = await page.evaluate(() => ({
    cells: document.querySelectorAll('.cell').length,
    errors: [...document.querySelectorAll('.cell .err')].map((e) => `${e.closest('.cell').dataset.type}/${e.closest('.cell').dataset.sample}: ${e.textContent.split('\n')[0]}`),
    states: [...document.querySelectorAll('.cell .oc-root')].length,
    charts: document.querySelectorAll('section.chart').length,
  }))
  total += drawn
  problems.push(...res.errors)
  console.log(`${theme.padEnd(14)} ${res.charts} chart types, ${res.cells} cells, ${drawn} rendered, ${res.errors.length} errors`)
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: join(BUILD, `gallery-${theme}.png`) })
}
await browser.close()
for (const p of problems.slice(0, 40)) console.log(`FAIL ${p}`)
console.log(problems.length ? `\n${problems.length} problem(s)` : `gallery: ${total} renders across all themes, no errors, no network`)
process.exit(problems.length ? 1 : 0)
