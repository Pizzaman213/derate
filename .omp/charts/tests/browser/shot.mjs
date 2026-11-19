#!/usr/bin/env node
// Look at a chart: bundle ONE chart type privately (so parallel work never races on
// dist/), render every sample in headless Chromium, write PNGs, run the layout checks.
//
//   node tests/browser/shot.mjs <id> [--samples basic,realistic] [--widths 320,768] [--themes light,dark] [--hover]
//
// PNGs: tests/.build/shots/<id>/<sample>-<theme>-<width>.png. Exit 1 on any finding.

import { build } from 'esbuild'
import { chromium } from 'playwright-core'
import { mkdirSync, readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { ROOT, BUILD, fontFaceCSS, argv } from './lib.mjs'
import { layoutCheck, markCenter, tooltipCheck } from './checks.mjs'

const a = argv()
const id = process.argv[2]
if (!id || id.startsWith('--')) { console.error('usage: node tests/browser/shot.mjs <id> [--samples a,b] [--widths 320,768,1280] [--themes light,dark,high-contrast] [--hover]'); process.exit(2) }
const dir = join(BUILD, 'shots', id)
mkdirSync(dir, { recursive: true })
const sampleDir = join(ROOT, 'types', id, 'samples')
const all = readdirSync(sampleDir).filter((f) => f.endsWith('.json')).map((f) => f.slice(0, -5))
const samples = a.get('samples')?.split(',') ?? all
const widths = (a.get('widths') ?? '640').split(',').map(Number)
const themes = (a.get('themes') ?? 'light').split(',')

const entry = join(dir, 'entry.ts')
writeFileSync(entry, `import mod from ${JSON.stringify(join(ROOT, 'types', id, 'index'))}\n(window as unknown as { __mod: typeof mod }).__mod = mod\n`)
await build({ entryPoints: [entry], bundle: true, outfile: join(dir, 'bundle.js'), format: 'iife', platform: 'browser', target: 'es2022', logLevel: 'error' })
const data = Object.fromEntries(samples.map((s) => [s, JSON.parse(readFileSync(join(sampleDir, `${s}.json`), 'utf8'))]))
writeFileSync(join(dir, 'page.html'), `<!doctype html><html lang="en"><head><meta charset="utf-8"><style>${fontFaceCSS()}body{margin:0}.frame{padding:12px}</style><script src="bundle.js"></script><script>window.__samples=${JSON.stringify(data)}</script></head><body><div id="app"></div></body></html>`)

const browser = await chromium.launch({ headless: true })
const page = await browser.newPage({ viewport: { width: Math.max(...widths) + 40, height: 900 } })
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
await page.goto(pathToFileURL(join(dir, 'page.html')).href)
await page.evaluate(() => Promise.all(['400 11px "IBM Plex Sans"', '500 11px "IBM Plex Sans"', '600 11px "IBM Plex Sans"', '400 11px "IBM Plex Mono"'].map((f) => document.fonts.load(f))))
let findings = 0
for (const s of samples) for (const theme of themes) for (const w of widths) {
  const r = await page.evaluate(({ s, theme, w }) => {
    const app = document.getElementById('app')
    window.__h?.destroy()
    app.innerHTML = `<div class="frame" id="frame"><div id="host" style="width:${w}px"></div></div>`
    const host = document.getElementById('host')
    const sample = window.__samples[s]
    try {
      window.__h = window.__mod.render(host, sample.data, { ...(sample.options ?? {}), theme, animate: false })
      document.getElementById('frame').style.background = getComputedStyle(window.__h.root).getPropertyValue('--oc-surface')
      return { state: window.__h.state, marks: window.__h.markCount(), renderer: window.__h.renderer, summary: window.__h.summary(), errors: window.__h.errors }
    } catch (e) { return { error: String(e.stack || e) } }
  }, { s, theme, w })
  const file = join(dir, `${s}-${theme}-${w}.png`)
  await page.locator('#frame').screenshot({ path: file })
  const f = r.error ? [r.error] : await page.evaluate(layoutCheck, '#host')
  if (a.has('hover') && r.state === 'ok' && r.renderer === 'svg') {
    const c = await page.evaluate(markCenter, ['#host', 0])
    if (c) { await page.mouse.move(c.x, c.y); f.push(...(await page.evaluate(tooltipCheck, '#host'))); await page.mouse.move(0, 0) }
  }
  findings += f.length
  console.log(`${f.length ? 'FAIL' : 'ok  '} ${s} ${theme} ${w}: ${r.state ?? 'threw'} ${r.marks ?? ''} marks ${r.renderer ?? ''} — ${r.summary ?? r.error ?? ''}${r.errors?.length ? ' ' + JSON.stringify(r.errors) : ''}`)
  for (const x of f) console.log(`     ${x}`)
}
if (errors.length) { console.log('page errors:'); for (const e of errors) console.log('  ' + e); findings += errors.length }
console.log(`\nPNGs in ${dir}`)
await browser.close()
process.exit(findings ? 1 : 0)
