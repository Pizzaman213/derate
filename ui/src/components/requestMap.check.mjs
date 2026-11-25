// requires: browser coordinator -- draws the request map on the real /usage screen
//
// The request map, in a real browser: every pane is mounted and hidden while
// inactive, so the map can be built inside a 0x0 box. Leaflet fitted its view
// to that box and re-measures only on a window resize, which drew zoom 1, the
// world's tiles below the frame and every marker as `d="M0 0"` -- a blank map
// that types and a screenshot of the wrong pane both pass. This opens Usage on
// Overview, switches to Requests, and checks that what is drawn is inside the
// frame; then hides and re-shows the pane, which is the other way to get there.
//
//   DERATE_CHECK_ORIGIN=http://127.0.0.1:18170 DERATE_CHECK_ADMIN_USER=... \
//     DERATE_CHECK_ADMIN_PASSWORD=... node src/components/requestMap.check.mjs

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

const MAP = '#us-requests .req-map'

/** What the map draws, measured in the page. */
const measure = (page) =>
  page.evaluate((sel) => {
    const map = document.querySelector(sel)
    if (!map) return null
    const box = map.getBoundingClientRect()
    const overlap = (r) =>
      Math.max(0, Math.min(r.right, box.right) - Math.max(r.left, box.left)) *
      Math.max(0, Math.min(r.bottom, box.bottom) - Math.max(r.top, box.top))
    const tiles = [...map.querySelectorAll('.leaflet-tile-loaded')].map((t) => overlap(t.getBoundingClientRect()))
    const markers = [...map.querySelectorAll('path.req-map-marker')].map((p) => ({
      d: p.getAttribute('d'),
      inFrame: overlap(p.getBoundingClientRect()) > 0,
    }))
    return {
      width: box.width,
      height: box.height,
      tiles: tiles.length,
      tilesInFrame: tiles.filter((a) => a > 0).length,
      markers,
    }
  }, MAP)

const verdict = (m, when) => {
  check(m !== null, `${when}: the map is on the page`)
  if (!m) return
  check(m.width > 0 && m.height > 0, `${when}: the map has a size (${Math.round(m.width)}x${Math.round(m.height)})`)
  check(m.tilesInFrame > 0, `${when}: a loaded tile is inside the frame (${m.tilesInFrame} of ${m.tiles})`)
  const blank = m.markers.filter((p) => p.d === 'M0 0')
  check(blank.length === 0, `${when}: no marker is drawn as "M0 0" (${blank.length} of ${m.markers.length})`)
  if (m.markers.length > 0) {
    // The view is fitted to the points, so every one of them is on screen.
    const outside = m.markers.filter((p) => !p.inFrame)
    check(outside.length === 0, `${when}: every marker is inside the frame (${outside.length} of ${m.markers.length} outside)`)
  } else {
    note(`${when}: no origins in this window, so no markers to check`)
  }
}

const engine = await chromium.launch({ executablePath: browser.path })
const page = await engine.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
await page.context().addCookies([sessionCookie])

try {
  await page.goto(`${ORIGIN}/usage`, { waitUntil: 'networkidle' })
  await page.click('#us-tab-requests')
  await page.locator(`${MAP} .leaflet-tile-loaded`).first().waitFor({ timeout: 20000 })
  // Tiles fade in and markers land after the origins answer; let both settle.
  await page.waitForTimeout(1000)
  verdict(await measure(page), 'first shown')

  // Hide it and show it again: the map is built, and the pane goes 0x0 between.
  await page.click('#us-tab-overview')
  await page.waitForTimeout(500)
  await page.click('#us-tab-requests')
  await page.locator(`${MAP} .leaflet-tile-loaded`).first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(1000)
  verdict(await measure(page), 'shown again')

  // Resize the window while the pane is hidden. Leaflet's own resize handler
  // measures the hidden map at 0x0 and nothing re-measures it on the way back
  // unless the container is watched.
  await page.click('#us-tab-overview')
  await page.setViewportSize({ width: 1024, height: 800 })
  await page.waitForTimeout(500)
  await page.click('#us-tab-requests')
  await page.locator(`${MAP} .leaflet-tile-loaded`).first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(1000)
  verdict(await measure(page), 'shown after a resize while hidden')
} finally {
  await engine.close()
}

done()
