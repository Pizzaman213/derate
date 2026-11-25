// requires: browser coordinator -- drives the real Serve and Advanced tabs
//
// Every control on a model's Serve tab (the planner) and Advanced tab, in a
// real browser against a live coordinator, with every `/api/plan` it sends
// recorded: what was asked, what came back, and whether the verdict card the
// controls live inside survived the answer. That last one is the trap this
// screen keeps falling into -- a 400 nulls `result`, and the card, the picker
// and every control drawn inside it unmount with it.
//
// What each section proves, against the server's own answer rather than
// against source:
//   * the planner's sentence states the context and concurrency the verdict
//     was taken at (it said "32768 context and concurrency 1" under a verdict
//     at 40960 and 16);
//   * "Why N GiB?" adds up to N;
//   * who picks the nodes, and ticking them, re-plan and keep the card;
//   * every engine the picker enables keeps the card;
//   * every KV width with a multiplier plans, priced at exactly that
//     multiplier, and every dashed one is disabled;
//   * context and sequences reach the request and the command preview;
//   * nothing on the page throws, nothing from the coordinator is a 5xx, and
//     at 390 px wide nothing scrolls sideways.
//
//   DERATE_CHECK_ORIGIN=http://localhost:18120 DERATE_CHECK_ADMIN_USER=... \
//     DERATE_CHECK_ADMIN_PASSWORD=... node src/tabs/models/servePanel.check.mjs

import { ORIGIN, sessionCookie } from '../../check/auth.mjs'
import { chromium } from 'playwright-core'
import { findBrowser } from '../../check/browser.mjs'
import { report } from '../../check/harness.mjs'

const { check, note, done } = report()

/** Small, ungated, served by vllm here: every control is live for it. */
const MODEL = 'Qwen/Qwen3-0.6B'
/** The resolve and the hub enumeration on a cold coordinator. */
const SLOW = 180_000
const GIB = 1024 ** 3

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
const page = await engine.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' })
await page.context().addCookies([sessionCookie])

/** Every plan the page asked for, in order: request body, status, answer. */
const plans = []
const failures = []
const pageErrors = []
page.on('pageerror', (e) => pageErrors.push(String(e)))
page.on('console', (m) => {
  if (m.type() === 'error') pageErrors.push(m.text())
})
page.on('response', async (r) => {
  if (!r.url().startsWith(ORIGIN)) return
  if (r.status() >= 500) failures.push(`${r.status()} ${r.url()}`)
  if (!r.url().endsWith('/api/plan') || r.request().method() !== 'POST') return
  let json = null
  try {
    json = await r.json()
  } catch {
    // An aborted request has no body; the next one will.
  }
  plans.push({ body: r.request().postDataJSON(), status: r.status(), json })
})

/** Run *act*, then wait until the page has settled on a plan it sent after. */
async function replan(act) {
  const before = plans.length
  await act()
  const deadline = Date.now() + 30_000
  while (plans.length === before && Date.now() < deadline) await page.waitForTimeout(150)
  // The panel debounces and may send more than one; take the last.
  let seen = plans.length
  for (;;) {
    await page.waitForTimeout(700)
    if (plans.length === seen) break
    seen = plans.length
  }
  return plans.length > before ? plans[plans.length - 1] : null
}

const cardMounted = () => page.locator('.mdl-verdict').first().isVisible()

/** The radio inside a `<label>` whose text starts with *text*. */
const radioLabelled = (scope, text) =>
  scope.locator('label').filter({ hasText: new RegExp(`^\\s*${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`) }).locator('input[type="radio"]').first()

try {
  await page.goto(`${ORIGIN}/models/${MODEL}`, { waitUntil: 'domcontentloaded' })
  await page.getByText('options on the Hub').first().waitFor({ timeout: SLOW })
  await page.locator('.mdl-verdict').first().waitFor({ timeout: SLOW })
  await page.waitForTimeout(1500)

  // -- the planner ----------------------------------------------------------
  const first = plans.find((p) => p.status === 200 && !p.body.kv_dtype)
  check(first != null, `the Serve tab planned ${MODEL} (${plans.length} plan requests)`)
  const verdict = first?.json
  if (verdict) {
    const sentences = [verdict.plan?.reason, verdict.recommended_plan?.reason].filter(Boolean)
    const stated = sentences.flatMap((s) => [...s.matchAll(/at (\d+) context and concurrency (\d+)/g)])
    check(
      stated.every((m) => Number(m[1]) === verdict.context && Number(m[2]) === verdict.concurrency),
      `the planner's sentence states the verdict's own context and concurrency (${verdict.context}, ${verdict.concurrency}): ` +
        (stated.map((m) => m[0]).join('; ') || 'no figures stated'),
    )

    const why = page.locator('.mdl-textbtn', { hasText: /^Why / }).first()
    const total = Number((await why.innerText()).match(/([\d.]+) GiB/)?.[1])
    await why.click()
    // The terms only: the budget sentences beside them carry their own GiB.
    const parts = (await page.locator('.mdl-terms dd.mono').allInnerTexts()).map((t) => Number(t.match(/([\d.]+) GiB/)?.[1]))
    const sum = parts.reduce((a, b) => a + b, 0)
    check(
      parts.length >= 2 && Math.abs(sum - total) <= 0.05 * parts.length,
      `"Why ${total} GiB?" adds up: ${parts.join(' + ')} = ${sum.toFixed(1)}`,
    )
    const breakdown = verdict.fit?.breakdown
    check(
      breakdown != null && Math.abs(breakdown.total / GIB - total) < 0.06,
      `the figure on the button is the gate's total (${breakdown ? (breakdown.total / GIB).toFixed(2) : '?'} GiB)`,
    )
  }

  const picks = page.getByRole('combobox', { name: 'Who picks the nodes' })
  for (const choice of ['You picked', 'Planner picks']) {
    const p = await replan(async () => {
      await picks.click()
      await page.getByRole('option', { name: choice }).click()
    })
    check(await cardMounted(), `"${choice}": the verdict card is still on screen (plan ${p?.status ?? 'not re-sent'})`)
  }

  const tick = page.locator('.mdl-node input[type="checkbox"]').first()
  if (!(await tick.count())) {
    note('no node row on this coordinator')
  } else if (!(await tick.isEnabled())) {
    // One machine: it is the plan, and unticking it would plan on nothing.
    check(
      (await tick.isChecked()) && Boolean(await tick.getAttribute('title')),
      `the only machine stays ticked and says why ("${await tick.getAttribute('title')}")`,
    )
  } else {
    const p = await replan(() => tick.click())
    check(await cardMounted(), `unticking a node keeps the verdict card (plan ${p?.status ?? 'not re-sent'})`)
    await replan(() => tick.click())
    check(await cardMounted(), 'ticking it again keeps the verdict card')
  }

  // -- the Advanced tab -----------------------------------------------------
  await page.getByRole('tab', { name: 'Advanced' }).click()
  await page.waitForTimeout(500)
  for (const label of ['Speed', 'Memory', 'Context', 'Parallelism', 'API', 'Lifecycle', 'Custom flags']) {
    const head = page.locator('button[aria-expanded="false"]', { hasText: new RegExp(`^${label}`) }).first()
    if (await head.count()) await head.click()
  }

  for (const name of ['SGLang', 'TTS server', 'vLLM']) {
    // An engine row's accessible name is its label, after any monogram mark.
    const radio = page.getByRole('radio', { name: new RegExp(`(^|\\s)${name}\\b`) }).first()
    if (!(await radio.count())) {
      note(`no ${name} engine row`)
      continue
    }
    if (!(await radio.isEnabled())) {
      note(`${name} is disabled for this model`)
      continue
    }
    const p = await replan(() => radio.check())
    check(await cardMounted(), `engine ${name}: the verdict card is still on screen (plan ${p?.status ?? 'not re-sent'})`)
  }

  const kvGroup = page.getByRole('radiogroup', { name: 'KV cache precision' })
  const base = plans.filter((p) => p.status === 200 && !p.body.kv_dtype && p.body.runtime !== 'sglang').at(-1)
  const ratios = base?.json?.kv_dtype_ratios ?? {}
  const sixteen = base?.json?.fit?.breakdown?.kv_cache
  const rows = kvGroup.locator('label')
  const count = await rows.count()
  let offered = 0
  for (let i = 0; i < count; i++) {
    const row = rows.nth(i)
    const radio = row.locator('input[type="radio"]')
    const name = (await row.locator('span').first().innerText()).trim()
    const text = await row.innerText()
    const dashed = text.includes('—') && !text.includes('×')
    if (name.startsWith('The model')) continue
    if (dashed) {
      check(!(await radio.isEnabled()), `KV ${name}: dashed, so it cannot be picked`)
      continue
    }
    offered += 1
    const p = await replan(() => radio.check())
    const ok = p?.status === 200
    const ratio = ratios[name]
    const kv = p?.json?.fit?.breakdown?.kv_cache
    check(
      ok && (await cardMounted()) && ratio != null && sixteen != null && Math.abs(kv - sixteen * ratio) <= sixteen * 1e-6,
      `KV ${name}: plans (${p?.status}), the card stays, priced at ×${ratio?.toFixed(2)} (${kv} of ${sixteen})`,
    )
  }
  check(offered > 0, `the picker offered ${offered} KV widths`)
  await replan(() => radioLabelled(kvGroup, 'The model').check())

  const p8k = await replan(() => page.getByRole('button', { name: '8k', exact: true }).last().click())
  check(p8k?.body?.context === 8192 && p8k?.json?.context === 8192, `8k reaches the plan (${p8k?.body?.context} -> ${p8k?.json?.context})`)
  const preview = () => page.locator('.mdl-preview .cmd').first().innerText()
  check((await preview()).replace(/\s*\\\s*/g, ' ').includes('--max-model-len 8192'), 'the command preview carries --max-model-len 8192')

  const seqs = page.getByLabel('Sequences', { exact: true })
  const p4 = await replan(async () => {
    await seqs.fill('4')
    await seqs.press('Tab')
  })
  check(p4?.body?.concurrency === 4 && p4?.json?.concurrency === 4, `4 sequences reach the plan (${p4?.body?.concurrency} -> ${p4?.json?.concurrency})`)
  check((await preview()).includes('max_num_seqs=4'), 'the command preview carries max_num_seqs=4')

  // An engine option re-plans: it is a fit-gate input.
  const asyncBox = page.getByRole('checkbox', { name: /Async scheduling/ })
  if (await asyncBox.count()) {
    const p = await replan(() => asyncBox.check())
    check(
      (await cardMounted()) && p?.status === 200 && p.body?.engine_options?.async_scheduling === true,
      `Async scheduling re-plans with engine_options.async_scheduling (plan ${p?.status ?? 'not re-sent'})`,
    )
    await replan(() => asyncBox.uncheck())
  } else {
    note('no "Async scheduling" control')
  }

  // A launch-only flag does not re-plan, and is exactly what the preview must
  // still show: it rendered the PLAN body until 2026-11-23 and left it out.
  const commandText = async () =>
    (await preview()) + '\n' + (await page.locator('.mdl-recipe').first().textContent()).toString()
  const eager = page.getByRole('checkbox', { name: /Eager mode/ })
  if (await eager.count()) {
    await eager.check()
    let shown = false
    for (let i = 0; i < 40 && !shown; i++) {
      await page.waitForTimeout(250)
      shown = (await commandText()).includes('enforce-eager') || (await commandText()).includes('enforce_eager')
    }
    check(shown, 'Eager mode reaches the command derate will run')
    await eager.uncheck()
  } else {
    note('no "Eager mode" control')
  }

  // -- phone width ----------------------------------------------------------
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('tab', { name: 'Serve' }).click()
  await page.waitForTimeout(1000)
  const overflow = await page.evaluate(() => {
    const doc = document.scrollingElement
    const wide = [...document.querySelectorAll('.mdl-detail *')]
      .filter((e) => e.getBoundingClientRect().right > window.innerWidth + 1 && getComputedStyle(e).position !== 'fixed')
      .slice(0, 3)
      .map((e) => `${e.tagName.toLowerCase()}.${[...e.classList].join('.')}`)
    return { scroll: doc.scrollWidth, width: doc.clientWidth, wide }
  })
  check(overflow.scroll <= overflow.width, `at 390 px nothing scrolls sideways (${overflow.scroll} > ${overflow.width}: ${overflow.wide.join(', ')})`)

  check(pageErrors.length === 0, `no page or console error (${pageErrors.slice(0, 3).join(' | ')})`)
  check(failures.length === 0, `nothing from the coordinator was a 5xx (${failures.slice(0, 3).join(', ')})`)
} finally {
  await engine.close()
}
done()
