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

  const cache = process.env.PLAYWRIGHT_BROWSERS_PATH || join(homedir(), '.cache', 'ms-playwright')
  if (!existsSync(cache)) return { path: null, why: `no browser cache at ${cache}` }

  // Full chromium first, headless_shell second: the shell is smaller and
  // faster but cannot do everything, and when both are present the full
  // browser is the one that renders what a person would actually see.
  for (const [prefix, exe] of [['chromium-', 'chrome'], ['chromium_headless_shell-', 'headless_shell']]) {
    const dirs = readdirSync(cache)
      .filter((d) => d.startsWith(prefix))
      .sort((a, b) => revision(b) - revision(a))
    for (const d of dirs) {
      const p = join(cache, d, 'chrome-linux', exe)
      if (existsSync(p)) return { path: p, why: d }
    }
  }
  return { path: null, why: `no chromium under ${cache}` }
}
