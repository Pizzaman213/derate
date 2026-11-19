// What every browser-side verifier in the pack shares: build the gallery,
// serve it, launch the Chromium already in the Playwright cache (never a
// download -- ui/src/check/browser.mjs::findBrowser is the one rule for that),
// collect console errors, inject axe-core.
//
//   const g = await openGallery({ query: 'only=Readout&theme=dark' })
//   … g.page, g.errors, await g.axe(selector) …
//   await g.close()
//
// The gallery is served over a loopback HTTP server rather than opened from
// file://: the router uses history.pushState, which Chromium refuses on an
// opaque file: origin, and relative fetches resolve sanely. External requests
// (publisher avatars from huggingface.co) are aborted so the checks never
// depend on the network, and the resulting resource errors are not counted.

import { createServer } from 'node:http'
import { readFileSync, existsSync, statSync, readdirSync } from 'node:fs'
import { join, extname, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright-core'
import { findBrowser } from '../../../../ui/src/check/browser.mjs'
import { buildGallery, OUT } from '../gallery/build.mjs'

const here = dirname(fileURLToPath(import.meta.url))
export const AXE = resolve(here, '..', '..', '..', 'node_modules', 'axe-core', 'axe.min.js')
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.woff2': 'font/woff2', '.woff': 'font/woff', '.svg': 'image/svg+xml', '.png': 'image/png', '.md': 'text/markdown' }

export function serve(root) {
  const server = createServer((req, res) => {
    const path = decodeURIComponent(new URL(req.url, 'http://x').pathname)
    let file = join(root, path === '/' ? 'index.html' : path)
    if (!file.startsWith(root) || !existsSync(file) || statSync(file).isDirectory()) { res.writeHead(404); res.end('not found'); return }
    res.writeHead(200, { 'content-type': MIME[extname(file)] ?? 'application/octet-stream' })
    res.end(readFileSync(file))
  })
  return new Promise((ok) => server.listen(0, '127.0.0.1', () => ok({ server, url: `http://127.0.0.1:${server.address().port}` })))
}

let building = null
export function ensureGallery() {
  // One build per process, even when cells open the gallery concurrently.
  if (!building) building = buildGallery()
  return building
}

export async function launch(options = {}) {
  const b = findBrowser()
  if (!b.path) throw new Error(`no browser: ${b.why}`)
  return chromium.launch({ executablePath: b.path, ...options })
}

/** Open the gallery with a query string. Returns page, the console error list
 *  (filtered of aborted external resources), an axe runner and close(). */
export async function openGallery({ query = '', viewport = { width: 1280, height: 900 }, reducedMotion = 'no-preference', browser = null, root = null } = {}) {
  const dir = root ?? (await ensureGallery())
  const { server, url } = await serve(dir)
  const ownBrowser = browser === null
  const br = browser ?? (await launch())
  const context = await br.newContext({ viewport, reducedMotion })
  const page = await context.newPage()
  const errors = []
  page.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource|net::ERR_FAILED|net::ERR_ABORTED/.test(m.text())) errors.push(m.text()) })
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  await page.route('**/*', (route) => (route.request().url().startsWith(url) ? route.continue() : route.abort()))
  await page.goto(`${url}/index.html${query ? '?' + query : ''}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-gallery], [data-gallery-empty]', { timeout: 15000 }).catch(() => {})
  const axe = async (selector) => {
    if (!(await page.evaluate(() => 'axe' in window))) await page.addScriptTag({ path: AXE })
    return page.evaluate(async (sel) => {
      const target = sel ? document.querySelector(sel) : document
      return window.axe.run(target ?? document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag22aa'] }, resultTypes: ['violations'] })
    }, selector)
  }
  const close = async () => { await context.close(); if (ownBrowser) await br.close(); server.close() }
  return { page, errors, axe, close, url, browser: br }
}

export function sections(page) {
  return page.$$eval('[data-gallery]', (els) => els.map((e) => ({ component: e.dataset.component, variant: e.dataset.variant, state: e.dataset.state, interactive: e.dataset.interactive === 'true', width: e.getBoundingClientRect().width, height: e.getBoundingClientRect().height })))
}

export const THEMES = ['light', 'dark', 'high-contrast']
export const VIEWPORTS = [320, 768, 1280, 1920]
export function galleryStates() {
  // The registry drives which components exist; states.tsx is what mounts.
  return JSON.parse(readFileSync(resolve(here, '..', '..', '..', 'design', 'components', 'registry.json'), 'utf8')).components
}
export function checkFiles() {
  return readdirSync(join(here, '..', 'components')).filter((f) => f.endsWith('.check.mjs'))
}
