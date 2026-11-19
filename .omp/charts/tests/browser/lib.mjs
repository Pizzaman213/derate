// Shared Playwright plumbing for the browser suites: one headless Chromium, a page
// that loads dist/charts.iife.js + dist/charts-data.js + the bundled Plex fonts from
// disk (no network), and helpers to render a chart sample into a sized, themed host.

import { chromium } from 'playwright-core'
import { mkdirSync, writeFileSync, readFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
export const BUILD = join(ROOT, 'tests', '.build')

export function fontFaceCSS(inline = false) {
  const face = (file, weight, family = 'IBM Plex Sans') => {
    const p = join(ROOT, 'theme', 'fonts', file)
    const src = inline ? `data:font/woff2;base64,${readFileSync(p).toString('base64')}` : pathToFileURL(p).href
    return `@font-face{font-family:'${family}';font-style:normal;font-weight:${weight};font-display:block;src:url(${src}) format('woff2')}`
  }
  return [face('ibm-plex-sans-latin-400-normal.woff2', 400), face('ibm-plex-sans-latin-500-normal.woff2', 500), face('ibm-plex-sans-latin-600-normal.woff2', 600), face('ibm-plex-mono-latin-400-normal.woff2', 400, 'IBM Plex Mono')].join('\n')
}

export function pageHTML() {
  const dist = (f) => pathToFileURL(join(ROOT, 'dist', f)).href
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>charts</title>
<style>${fontFaceCSS()}
html,body{margin:0;padding:0;background:#fff;font-family:'IBM Plex Sans',sans-serif}
.host{box-sizing:border-box;padding:0;margin:0}
.frame{padding:12px}
</style>
<script src="${dist('charts.iife.js')}"></script>
<script src="${dist('charts-data.js')}"></script>
</head><body><div id="app"></div></body></html>`
}

export async function launch() {
  if (!existsSync(join(ROOT, 'dist', 'charts.iife.js'))) throw new Error('dist/ is missing — run: npm run charts:build')
  mkdirSync(BUILD, { recursive: true })
  const file = join(BUILD, 'page.html')
  writeFileSync(file, pageHTML())
  const browser = await chromium.launch({ headless: true })
  return { browser, url: pathToFileURL(file).href }
}

export async function newPage(browser, url, { width = 1280, height = 900, dpr = 1, reducedMotion = 'reduce' } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: dpr, reducedMotion })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(String(e)))
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('request', (r) => { if (!r.url().startsWith('file:') && !r.url().startsWith('data:') && !r.url().startsWith('blob:')) errors.push(`network request: ${r.url()}`) })
  await page.goto(url)
  // fonts load lazily; load every face up front so the first chart measures with the real font
  await page.evaluate(() => Promise.all(['400 11px "IBM Plex Sans"', '500 11px "IBM Plex Sans"', '600 11px "IBM Plex Sans"', '400 11px "IBM Plex Mono"'].map((f) => document.fonts.load(f))))
  return { page, ctx, errors }
}

/**
 * Render chart `type` with sample `sample` into a fresh host of `width` px, themed.
 * Resolves with { ok, state, marks, renderer, summary, ms, error }.
 */
export async function renderIn(page, { type, sample, width, height, theme = 'light', options = {} }) {
  return page.evaluate(({ type, sample, width, height, theme, options }) => {
    const app = document.getElementById('app')
    for (const h of window.__handles ?? []) h.destroy()
    window.__handles = []
    app.replaceChildren()
    document.documentElement.setAttribute('data-theme', theme)
    document.body.style.background = getComputedStyle(document.documentElement).getPropertyValue('--x') || ''
    const frame = document.createElement('div')
    frame.className = 'frame'
    frame.id = 'frame'
    const host = document.createElement('div')
    host.className = 'host'
    host.id = 'host'
    host.style.width = `${width}px`
    if (height) host.style.height = `${height}px`
    frame.appendChild(host)
    app.appendChild(frame)
    try {
      const s = OmpCharts.getSample(type, sample)
      const t0 = performance.now()
      const h = OmpCharts.chart(type, host, s.data, { ...(s.options ?? {}), ...options })
      const ms = performance.now() - t0
      window.__handles.push(h)
      window.__h = h
      const bg = getComputedStyle(h.root).getPropertyValue('--oc-surface').trim()
      frame.style.background = bg
      document.body.style.background = bg
      return { ok: true, state: h.state, marks: h.markCount(), renderer: h.renderer, summary: h.summary(), ms }
    } catch (e) {
      return { ok: false, error: String(e && e.stack || e) }
    }
  }, { type, sample, width, height, theme, options })
}

/** Every chart id and its sample names, from the loaded bundle. */
export async function inventory(page) {
  return page.evaluate(() => OmpCharts.listCharts().map((c) => ({ id: c.id, category: c.category, zoom: c.zoom, samples: OmpCharts.listSamples(c.id) })))
}

/** Parse --flag value / --flag a,b from argv. */
export function argv() {
  const a = process.argv.slice(2)
  const get = (k) => { const i = a.indexOf(`--${k}`); return i >= 0 ? a[i + 1] : undefined }
  return { get, has: (k) => a.includes(`--${k}`) }
}
