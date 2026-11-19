// requires: browser
// Render the production settings card over a scripted HTTP boundary. Assert
// attribution and recovery, not source text or component wiring.
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { dirname, resolve, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { serve, launch, AXE } from './lib/browser.mjs'
import { caseSensitiveRelative, EXTENSIONS } from './lib/esbuild.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const repo = resolve(here, '../../..')
const scratch = mkdtempSync(join(here, '.provider-status-'))
const provider = {
  provider_id: 'claude_code', kind: 'claude_code', display_name: 'Anthropic plan (unofficial)',
  sign_in_account: 'operator@example.test', api_key_ref: '', key_state: 'set', key_source: 'oauth',
  enabled: true, healthy: true, admitting: true, admission_block: null, last_error: null,
  model_count: 3, catalogue_count: 15, spend_today_usd: 0, last_refreshed: 0,
  models: ['claude-fable-5-2', 'claude-opus-5-5', 'claude-haiku-5-5'].map((name, i) => ({
    upstream_id: name, served_name: name, context_length: 200000,
    input_cost_per_mtok: null, output_cost_per_mtok: null,
    admitting: i !== 0, admission_block: i === 0 ? 'rate limited for another 243s' : null,
    retry_in_s: i === 0 ? 243 : 0,
    last_error: i === 0 ? "This request would exceed your account's rate limit. Please try again later." : null,
  })),
}
let host, br
try {
  writeFileSync(join(scratch, 'main.tsx'), `
import React from 'react'
import { createRoot } from 'react-dom/client'
import { ProvidersCard } from '${repo}/ui/src/tabs/settings/ProvidersCard'
import { UsageTab } from '${repo}/ui/src/tabs/UsageTab'
import { RouterProvider, useRouter } from '${repo}/ui/src/state/router'
import '${repo}/.omp/design/tokens/tokens.css'
import '${repo}/ui/src/styles/base.css'
import '${repo}/ui/src/styles/derate.css'
window.providerStatus = ${JSON.stringify(provider)}
window.rateCalls = []
const record = (id, model, status) => ({
  request_id: id, ts: Date.now() / 1000 - 60, served_name: model, model_requested: model,
  status, target_id: 'claude_code:' + model, target_kind: 'remote',
  provider_id: 'claude_code', attempts: 1, error_class: status === 429 ? 'RateLimitError' : '',
  error_code: '', node_id: 'coordinator', admission_code: '', prompt_tokens: 4, completion_tokens: 0,
  attempt_chain: [{ target_id: 'claude_code:' + model, status, ms: 150,
    error_message: status === 429 ? 'Recorded quota rejection for ' + model : null,
    retry_after_s: status === 429 ? 300 : null }],
})
window.rateRecords = [
  record('r-opus-limited', 'claude-opus-5-5', 429),
  record('r-opus-ok', 'claude-opus-5-5', 200),
  record('r-opus-unavailable', 'claude-opus-5-5', 503),
  record('r-fable-limited', 'claude-fable-5-2', 429),
]
window.fetch = async (input) => {
  const url = new URL(typeof input === 'string' ? input : input.url, location.href)
  const path = url.pathname
  window.rateCalls.push(path + url.search)
  let data = []
  if (path === '/api/providers') data = [window.providerStatus]
  else if (path === '/api/providers/secret-refs') data = { refs: [] }
  else if (path === '/api/providers/kinds') data = [{ kind: 'claude_code', sign_in: 'claude', display_name: 'Anthropic plan (unofficial)' }]
  else if (path === '/api/chat/models') data = { data: window.providerStatus.models.map((m) => ({ id: m.served_name })) }
  else if (path === '/api/cluster') data = { nodes: [], links: [], summary: {} }
  else if (path === '/api/settings') data = {}
  else if (path === '/api/usage') data = {
    rows: [
      { bucket: 1, group: { model: 'claude-opus-5-5', provider: 'claude_code' }, requests: 3, failed: 2 },
      { bucket: 2, group: { model: 'claude-opus-5-5', provider: 'claude_code' }, requests: 4, failed: 1 },
      { bucket: 2, group: { model: 'claude-fable-5-2', provider: 'claude_code' }, requests: 2, failed: 1 },
      { bucket: 2, group: { model: 'gpt-5.6-terra', provider: 'other-provider' }, requests: 5, failed: 2 },
      { bucket: 2, group: { model: 'gpt-6.2-sol', provider: 'claude_code' }, requests: 8, failed: 0 },
    ], group_by: ['model', 'provider'], from: Date.now()/1000 - 86400, to: Date.now()/1000, gaps: [], durable: true,
  }
  else if (path === '/api/requests') {
    let rows = window.rateRecords
    for (const [param, field] of [['request_id','request_id'], ['provider_id','provider_id'], ['model','served_name'], ['target_id','target_id']]) {
      if (url.searchParams.get(param)) rows = rows.filter((r) => r[field] === url.searchParams.get(param))
    }
    const status = url.searchParams.get('status')
    if (status === 'ok') rows = rows.filter((r) => r.status < 400)
    else if (status === 'failed') rows = rows.filter((r) => r.status >= 400)
    else if (status) rows = rows.filter((r) => r.status === Number(status))
    const size = Number(url.searchParams.get('page_size') || 100)
    const page = Number(url.searchParams.get('page') || 1)
    data = { total: rows.length, rows: rows.slice((page-1)*size, page*size), page, page_size: size }
  }
  return new Response(JSON.stringify(data), { headers: { 'content-type': 'application/json' } })
}
const initialUsage = new URLSearchParams(location.search).get('view') === 'usage'
history.replaceState(null, '', initialUsage ? '/usage?dep=claude-opus-5-5' : '/settings')
function Fixture() {
  const { route } = useRouter()
  return route.dest === 'usage' ? <UsageTab /> : <ProvidersCard />
}
const root = createRoot(document.getElementById('root'))
let revision = 0
window.renderProviderStatus = () => root.render(<RouterProvider><Fixture key={++revision} /></RouterProvider>)
window.renderProviderStatus()
`)
  const uiMod = (name) => join(repo, 'ui/node_modules', name)
  await build({
    entryPoints: [join(scratch, 'main.tsx')], outfile: join(scratch, 'bundle.js'),
    bundle: true, jsx: 'automatic', absWorkingDir: join(repo, 'ui'),
    alias: { react: uiMod('react'), 'react-dom': uiMod('react-dom') },
    resolveExtensions: EXTENSIONS, plugins: [caseSensitiveRelative],
    loader: { '.woff2': 'dataurl', '.woff': 'dataurl' },
  })
  writeFileSync(join(scratch, 'index.html'), '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Provider model status</title><link rel="stylesheet" href="/bundle.css"><main id="root"></main><script type="module" src="/bundle.js"></script></html>')
  host = await serve(scratch)
  if (process.argv.includes('--serve')) {
    console.log(`Provider status preview ready: ${host.url}`)
    await new Promise((done) => { process.once('SIGTERM', done); process.once('SIGINT', done) })
  } else {
    br = await launch()
    const page = await br.newPage({ viewport: { width: 1440, height: 900 } })
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(host.url)
    const table = page.getByRole('table', { name: 'Model status for Anthropic plan (unofficial)', exact: true })
    await table.waitFor()
    const fable = table.getByRole('row').filter({ hasText: 'claude-fable-5-2' })
    const opus = table.getByRole('row').filter({ hasText: 'claude-opus-5-5' })
    const haiku = table.getByRole('row').filter({ hasText: 'claude-haiku-5-5' })
    assert.match(await fable.innerText(), /243s/)
    assert.match(await fable.innerText(), /exceed your account's rate limit/)
    assert.match(await opus.innerText(), /admitting/)
    assert.match(await haiku.innerText(), /admitting/)
    assert.doesNotMatch(await opus.innerText(), /rate limit|retry/)
    const providerRow = page.locator('tr').filter({ has: page.getByRole('cell', { name: 'Anthropic plan (unofficial)', exact: true }) })
    assert.doesNotMatch(await providerRow.innerText(), /rate limit|retrying/)
    const toggle = page.getByRole('button', { name: 'Model status for Anthropic plan (unofficial)', exact: true })
    await toggle.focus(); await page.keyboard.press('Enter')
    assert.equal(await toggle.getAttribute('aria-expanded'), 'false')
    await page.keyboard.press('Enter'); await table.waitFor()
    await page.addScriptTag({ path: AXE })
    const a11y = await page.evaluate(async () => window.axe.run(document.querySelector('#root'), { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag22aa'] } }))
    assert.deepEqual(a11y.violations.map((v) => v.id), [])
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({ width, height: 900 })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `page fits ${width}px`)
    }
    await page.evaluate(() => {
      window.providerStatus.models.forEach((m) => { m.admitting = true; m.admission_block = null; m.last_error = null; m.retry_in_s = 0 })
      window.renderProviderStatus()
    })
    await table.waitFor({ state: 'hidden' })
    await page.getByRole('button', { name: 'Model status for Anthropic plan (unofficial)', exact: true }).click()
    assert.doesNotMatch(await table.innerText(), /rate limited for|243s/)
    await page.evaluate(() => { window.providerStatus.healthy = false; window.providerStatus.last_error = 'could not reach upstream'; window.renderProviderStatus() })
    await page.getByText('unreachable', { exact: true }).waitFor()
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.getByRole('button', { name: 'Model status for Anthropic plan (unofficial)', exact: true }).click()
    await page.getByRole('link', { name: 'Rate-limit history for claude-opus-5-5', exact: true }).click()
    await page.getByRole('tab', { name: 'Rate limits', exact: true }).waitFor()
    assert.equal(new URL(page.url()).pathname, '/usage/rate-limits')
    assert.equal(new URL(page.url()).searchParams.get('dep'), 'claude-opus-5-5')
    const panel = page.locator('#us-rate-limits')
    const log = panel.getByRole('table', { name: 'Recorded rate limits', exact: true })
    await log.getByText('r-opus-limited', { exact: true }).waitFor()
    assert.doesNotMatch(await log.innerText(), /r-fable-limited|r-opus-unavailable/)
    assert.match(await log.innerText(), /300 s/)
    assert.match(await log.innerText(), /Recorded quota rejection for claude-opus-5-5/)
    await panel.locator('.oc-root svg').nth(1).waitFor()
    assert.equal(await panel.locator('dl').count(), 0, 'standalone numeric summary is removed')
    const errorChart = panel.locator('.oc-root').filter({ has: page.locator('.oc-title', { hasText: /^Request errors$/ }) })
    const modelChart = panel.locator('.oc-root').filter({ has: page.locator('.oc-title', { hasText: /^Errors by model$/ }) })
    const chartRows = (chart) => chart.locator('table tbody tr').evaluateAll((rows) =>
      rows.map((row) => Array.from(row.cells).slice(0, 2).map((cell) => cell.textContent)))
    assert.deepEqual(await chartRows(errorChart), [['Rate limited', '1'], ['Other errors', '1']])
    assert((await errorChart.locator('svg text').allTextContents()).includes('2'), 'successful requests do not enter the error total')
    assert.deepEqual(await chartRows(modelChart), [['claude-opus-5-5', '3'], ['claude-fable-5-2', '1']])
    assert((await modelChart.locator('svg text').allTextContents()).includes('4'), 'model error total folds every bucket, not the request page')
    await panel.getByRole('combobox', { name: 'Provider', exact: true }).selectOption('')
    assert.deepEqual(await chartRows(modelChart), [['claude-opus-5-5', '3'], ['gpt-5.6-terra', '2'], ['claude-fable-5-2', '1']])
    await panel.getByRole('combobox', { name: 'Provider', exact: true }).selectOption('claude_code')
    assert.equal(await modelChart.getByRole('list', { name: 'Legend', exact: true }).count(), 0, 'model legend is hidden without losing the accessible data table')
    const windowButtons = panel.getByRole('group', { name: 'Rate limit window', exact: true })
    await windowButtons.getByRole('button', { name: '24h', exact: true }).click()
    assert.equal(new URL(page.url()).searchParams.get('win'), '24h')
    assert.equal(await windowButtons.getByRole('button', { name: '24h', exact: true }).getAttribute('aria-pressed'), 'true')
    await windowButtons.getByRole('button', { name: '30d', exact: true }).click()
    await log.getByText('r-opus-limited', { exact: true }).waitFor()
    await page.addScriptTag({ path: AXE })
    const usageA11y = await page.evaluate(async () => window.axe.run(document.querySelector('#us-rate-limits'), { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag22aa'] } }))
    assert.deepEqual(usageA11y.violations.map((v) => v.id), [])
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `history fits ${width}px`)
    }
    await log.getByRole('link').click()
    const requests = page.locator('#us-requests')
    await requests.getByRole('heading', { name: 'r-opus-limited', exact: true }).waitFor()
    assert.match(await requests.innerText(), /rate limited/)
    assert.match(await requests.innerText(), /Recorded quota rejection for claude-opus-5-5/)
    assert.match(await requests.innerText(), /300 s/)
    await page.getByRole('tab', { name: 'Rate limits', exact: true }).click()
    await page.getByRole('tab', { name: 'Requests', exact: true }).click()
    await requests.getByRole('combobox', { name: 'Status', exact: true }).selectOption('429')
    await requests.getByText('rate limited', { exact: true }).first().waitFor()
    const filteredRows = await requests.locator('table tbody tr').allTextContents()
    assert(filteredRows.every((text) => text.includes('rate limited')), '429 filter excludes successful and other failed requests')
    assert.match(page.url(), /status=429/)
    const csv = requests.getByRole('link', { name: 'CSV', exact: true })
    assert.match(await csv.getAttribute('href'), /provider_id=claude_code/)
    assert.match(await csv.getAttribute('href'), /status=429/)
    await page.getByRole('tab', { name: 'Overview', exact: true }).click()
    assert.equal(new URL(page.url()).pathname, '/usage')
    // The supplied model link selects the model and opens the new pane.
    await page.goto(host.url + '/?view=usage')
    await page.locator('#us-rate-limits').getByRole('table', { name: 'Recorded rate limits', exact: true }).getByText('r-opus-limited', { exact: true }).waitFor()
    assert.equal(new URL(page.url()).pathname, '/usage/rate-limits')
    assert.equal(new URL(page.url()).searchParams.get('dep'), 'claude-opus-5-5')
    const calls = await page.evaluate(() => window.rateCalls.filter((u) => u.startsWith('/api/requests')))
    assert(calls.some((u) => u.includes('model=claude-opus-5-5') && u.includes('status=429')))
    assert.deepEqual(errors, [])
    console.log('PASS provider/model history: durable 429 attribution and stats, recovery, Usage deep link and tab navigation, recorded reason/retry delay, CSV filters, keyboard, accessibility and responsive overflow')
  }
} finally {
  if (br) await br.close()
  if (host) host.server.close()
  rmSync(scratch, { recursive: true, force: true })
}
