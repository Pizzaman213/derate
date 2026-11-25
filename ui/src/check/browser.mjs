// Where the Chromium is, or an honest answer that there isn't one.
//
// The screens verifier needs a real browser, and this repo will not download
// one at gate time: a check that reaches for the network to decide whether it
// can run is a check that fails for reasons that have nothing to do with the
// code. So the rule is use-what-is-here, and say so plainly when nothing is.
//
// Playwright keeps its browsers outside node_modules, in a shared cache, which
// is why `playwright-core` is the dependency rather than `playwright`:
// playwright-core never downloads anything, and passing an explicit
// executablePath means the driver's version never has to match the cached
// browser's revision.
//
// Order: $DERATE_CHECK_BROWSER, then the newest cached Chromium, then the
// headless_shell twin, then nothing -- and "nothing" is a SKIP the runner
// reports as a skip, never as a pass.
//
// Also imported by .omp/stubui/server.mjs (the agents' stub-UI MCP tool).

import { existsSync, readdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

/** The revision suffix as a number, so chromium-1234 sorts after chromium-999
 *  -- which a string sort gets wrong, and which would quietly pick a browser
 *  years older than the one that was installed for this. */
const revision = (name) => Number(name.replace(/^\D+/, '')) || 0

export function findBrowser() {
  const explicit = process.env.DERATE_CHECK_BROWSER
  if (explicit) {
    return existsSync(explicit)
      ? { path: explicit, why: '$DERATE_CHECK_BROWSER' }
      : { path: null, why: `$DERATE_CHECK_BROWSER=${explicit} does not exist` }
  }

  // Linux's cache, then macOS's. Whichever exists is the evidence; the OS name
  // is not consulted (see CLAUDE.md, "Platform questions are answered with
  // evidence").
  const cache = process.env.PLAYWRIGHT_BROWSERS_PATH
    || [join(homedir(), '.cache', 'ms-playwright'), join(homedir(), 'Library', 'Caches', 'ms-playwright')]
      .find((d) => existsSync(d))
    || join(homedir(), '.cache', 'ms-playwright')
  if (!existsSync(cache)) return { path: null, why: `no browser cache at ${cache}` }

  // Full chromium first, headless_shell second: the shell is smaller and
  // faster but cannot do everything, and when both are present the full
  // browser is the one that renders what a person would actually see.
  for (const [prefix, layouts] of LAYOUTS) {
    const dirs = readdirSync(cache)
      .filter((d) => d.startsWith(prefix))
      .sort((a, b) => revision(b) - revision(a))
    for (const d of dirs) {
      for (const rel of layouts) {
        const p = join(cache, d, ...rel)
        if (existsSync(p)) return { path: p, why: d }
      }
    }
  }
  return { path: null, why: `no chromium under ${cache}` }
}

/** Where the executable sits inside a revision directory. Playwright has used
 *  two layouts: `chrome-linux/chrome` (and `chrome-linux/headless_shell`) up to
 *  about 1.50, then Chrome for Testing's per-platform folders. This used to
 *  know only the first, so the browser `npx playwright install chromium`
 *  had just put on a fresh CI runner -- `chrome-linux64/chrome` -- read as no
 *  browser at all, and the screens verifier failed every publish. Both are
 *  listed: this box's cache predates the switch. Taken from playwright-core
 *  1.63's own registry (`executablePaths`). */
const LAYOUTS = [
  ['chromium-', [
    ['chrome-linux', 'chrome'],
    ['chrome-linux64', 'chrome'],
    ['chrome-linux-arm64', 'chrome'],
    ['chrome-mac-arm64', 'Google Chrome for Testing.app', 'Contents', 'MacOS', 'Google Chrome for Testing'],
    ['chrome-mac-x64', 'Google Chrome for Testing.app', 'Contents', 'MacOS', 'Google Chrome for Testing'],
  ]],
  ['chromium_headless_shell-', [
    ['chrome-linux', 'headless_shell'],
    ['chrome-headless-shell-linux64', 'chrome-headless-shell'],
    ['chrome-headless-shell-linux-arm64', 'chrome-headless-shell'],
    ['chrome-headless-shell-mac-arm64', 'chrome-headless-shell'],
    ['chrome-headless-shell-mac-x64', 'chrome-headless-shell'],
  ]],
]
