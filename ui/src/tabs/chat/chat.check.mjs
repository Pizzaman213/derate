// requires: browser coordinator -- the real Chat tab in a real browser
//
// The chat tab end to end, with the model side scripted: `/api/chat/models`,
// `/api/deployments` and `/api/chat/completions` are answered by `page.route`
// so the check does not depend on a model being up, and everything else --
// the bundle, auth, the router, the rest of the shell -- is the coordinator's.
//
// What it proves, in order: the head sentence and the chips come from the
// deployment's HF id and launch flags; the `on` chip puts the template switch
// in the request body; an inline `<think>` stream lands in the Thinking
// disclosure and only the answer renders as markdown; the meta block carries
// think time, tok/s and the verdict naming the parser to add; a second,
// server-separated turn is credited to the server, its body carries no switch
// and no reasoning in its history; the session strip appears; and the
// composer row with the chips still sits on one line at all three widths.
//
//   DERATE_CHECK_ORIGIN=http://localhost:18500 node src/tabs/chat/chat.check.mjs
//
// Writes ui/screens/chat-thinking.png for eyes.

import { sessionCookie } from '../../check/auth.mjs'
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright-core'
import { findBrowser } from '../../check/browser.mjs'
import { report } from '../../check/harness.mjs'
import { alignmentAudit } from '../../check/alignment.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const outDir = join(here, '..', '..', '..', 'screens')
const ORIGIN = process.env.DERATE_CHECK_ORIGIN ?? 'http://localhost:8088'

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

const MODEL = 'Qwen3-8B'
// A provider row: no deployment behind it, served over somebody's API.
const CLOUD = 'gpt-6.2-sol'
const MODELS = {
  object: 'list',
  data: [
    { id: MODEL, object: 'model', owned_by: 'derate', context_length: 32768, target_count: 1, target_kinds: ['local'], modality: 'text' },
    { id: CLOUD, object: 'model', owned_by: 'chatgpt', context_length: 400000, target_count: 1, target_kinds: ['remote'], modality: 'text' },
  ],
}
const DEPLOYMENT = {
  deployment_id: 'd-chatcheck',
  served_name: MODEL,
  model_id: 'Qwen/Qwen3-8B',
  runtime: 'vllm',
  state: 'ready',
  context_length: 32768,
  max_concurrent_seqs: 1,
  started_at: null,
  last_error: null,
  node_ids: [],
  modality: 'text',
  extra_args: [],
  plan: {
    kind: 'single_node', tensor_parallel: 1, pipeline_parallel: 1, expert_parallel: 1, data_parallel: 1,
    node_ids: [], reason: 'fixture', measured_link_gbps: 0, rejected: [],
  },
  fit: {
    verdict: 'fits',
    breakdown: { weights: 0, kv_cache: 0, activations: 0, comm_buffers: 0, replicated: 0, framework_overhead: 0, total: 0 },
    usable_per_node: 0, headroom: 0, reason: 'fixture', limiting_term: 'weights',
    max_context_that_fits: null, predicted_decode_tps: null, warnings: [],
  },
}

const sse = (frames) =>
  frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n'
const delta = (d, finish = null) => ({ choices: [{ index: 0, delta: d, finish_reason: finish }] })

// Turn 1: no reasoning parser -- the think block arrives inline in content.
const INLINE = sse([
  delta({ role: 'assistant' }),
  delta({ content: '<think>\n' }),
  delta({ content: 'x' }),
  delta({ content: '</think>\n\nY' }),
  delta({}, 'stop'),
])
// Turn 2: a parser separated it, and the last frame carries usage.
const SERVER = sse([
  delta({ reasoning: 'hm' }),
  delta({ content: '**Z**' }),
  delta({}, 'stop'),
  { choices: [], usage: { prompt_tokens: 20, completion_tokens: 5, completion_tokens_details: { reasoning_tokens: 3 } } },
])
// Turn 3: what a current Qwen checkpoint without a parser actually sends --
// the template opened `<think>` in the prompt, so only the close arrives.
const IMPLICIT = sse([
  delta({ role: 'assistant' }),
  delta({ content: 'The user said ' }),
  delta({ content: '"hi" again.' }),
  delta({ content: '\n</think>\n\n' }),
  delta({ content: "Hi! How's it going?" }),
  delta({}, 'stop'),
])
// Turn 5: a provider model streaming its reasoning on `delta.reasoning`, as
// OpenRouter does and the ChatGPT adapter now does.
const PROVIDER = sse([
  delta({ role: 'assistant' }),
  delta({ reasoning: 'Plan ' }),
  delta({ reasoning: 'the steps.' }),
  delta({ content: 'Done.' }),
  delta({}, 'stop'),
  { choices: [], usage: { prompt_tokens: 9, completion_tokens: 12, completion_tokens_details: { reasoning_tokens: 8 } } },
])

const bodies = []
const engine = await chromium.launch({ executablePath: browser.path })
const page = await engine.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce', colorScheme: 'dark' })
await page.context().addCookies([sessionCookie])
const errors = []
page.on('pageerror', (e) => errors.push(e.message))

const path = (url) => new URL(url).pathname
await page.route((url) => path(url.href) === '/api/chat/models', (route) => route.fulfill({ json: MODELS }))
await page.route((url) => path(url.href) === '/api/deployments', (route) => route.fulfill({ json: [DEPLOYMENT] }))
await page.route((url) => path(url.href) === '/api/chat/completions', (route) => {
  const body = route.request().postDataJSON()
  bodies.push(body)
  return route.fulfill({
    status: 200,
    headers: { 'content-type': 'text/event-stream', 'X-Request-Id': `r-chatcheck-${bodies.length}` },
    body: [INLINE, SERVER, IMPLICIT, IMPLICIT, PROVIDER][bodies.length - 1] ?? SERVER,
  })
})
// The live topology, plus the provider row: `?dep=` only holds a name that a
// deployment or a provider serves, so a click on a provider row with no
// remote here would fall back to the local model.
await page.route((url) => path(url.href) === '/api/topology', async (route) => {
  const live = await (await route.fetch()).json()
  const remote = {
    target_id: 't-cloud', provider_id: 'chatgpt', served_name: CLOUD, upstream_id: CLOUD,
    node_id: null, state: 'healthy', admitting: true, tokens_per_sec: 0, modality: 'text',
  }
  return route.fulfill({ json: { ...live, remotes: [...(live.remotes ?? []), remote] } })
})

await page.goto(`${ORIGIN}/chat?dep=${MODEL}`, { waitUntil: 'networkidle' })
const chips = page.locator('[role="group"][aria-label="Thinking"] button')
await chips.first().waitFor({ timeout: 15000 })

const top = await page.locator('.chattop').innerText()
check(JSON.stringify(await chips.allInnerTexts()) === JSON.stringify(['default', 'off', 'on', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']), 'chips are default · off · on · minimal · low · medium · high · xhigh · max')
check((await page.locator('.chatstats').count()) === 0, 'no session strip before anything was sent')
const setupLine = top.split('\n').find((l) => l.startsWith('Thinking:')) ?? top
check(setupLine.includes('Qwen3 switch (enable_thinking, model default on)') && setupLine.includes('parser: none'),
  `head names the family switch and the missing parser (${JSON.stringify(setupLine)})`)

// ── Turn 1: thinking on, inline <think> ─────────────────────────────────────
await chips.filter({ hasText: /^on$/ }).click()
check((await chips.filter({ hasText: /^on$/ }).getAttribute('aria-pressed')) === 'true', 'the on chip is pressed')
await page.locator('#chat-input').fill('hi')
await page.locator('#chat-input').press('Enter')
await page.locator('.turnmeta').first().waitFor({ timeout: 15000 })

check(bodies[0]?.chat_template_kwargs?.enable_thinking === true && bodies[0]?.chat_template_kwargs?.thinking === true,
  `the request carries the switch (${JSON.stringify(bodies[0]?.chat_template_kwargs)})`)
const disclosure = page.locator('.turn button[aria-expanded]').first()
const summary = (await disclosure.innerText()).trim()
check(summary.startsWith('Thinking ·'), `the thinking disclosure summarises the block (${JSON.stringify(summary)})`)
check((await disclosure.getAttribute('aria-expanded')) === 'false', 'it is closed once the answer has arrived')
await disclosure.click()
check((await page.locator('.think').first().innerText()) === 'x', 'the reasoning is in the disclosure')
check((await page.locator('.md').first().innerText()).trim() === 'Y', 'the answer is only the text after </think>')
const meta1 = await page.locator('.turnmeta').first().innerText()
check(meta1.includes('think ') && meta1.includes('tok/s') && meta1.includes('finish stop'), 'meta block has think time, rate and finish reason')
check(meta1.includes('--reasoning-parser qwen3') && meta1.includes('arrived inline'), 'verdict names the parser to add')
check(meta1.includes('(est.)'), 'counted tokens are labelled estimates')

// ── Turn 2: default, server-separated, with usage ───────────────────────────
await chips.filter({ hasText: /^default$/ }).click()
await page.locator('#chat-input').fill('again')
await page.locator('#chat-input').press('Enter')
await page.locator('.turnmeta').nth(1).waitFor({ timeout: 15000 })

check(bodies[1] && !('chat_template_kwargs' in bodies[1]) && !('reasoning_effort' in bodies[1]), 'default sends no switch')
check(JSON.stringify(bodies[1]?.messages) === JSON.stringify([
  { role: 'user', content: 'hi' }, { role: 'assistant', content: 'Y' }, { role: 'user', content: 'again' },
]), `history carries answers, never reasoning (${JSON.stringify(bodies[1]?.messages)})`)
const meta2 = await page.locator('.turnmeta').nth(1).innerText()
check(meta2.includes('separated by the server (parser unknown)'), 'server-separated reasoning is credited to the server')
check(meta2.includes('in 20') && meta2.includes('out 5') && meta2.includes('think 3 tok') && !meta2.includes('(est.)'),
  `usage figures are measured, not estimated (${JSON.stringify(meta2.split('\n')[1])})`)
check((await page.locator('.md strong').count()) === 1, 'the answer renders its markdown')

// ── Turn 3: default (Qwen3 thinks by default), only `</think>` arrives ──────
await page.locator('#chat-input').fill('hi')
await page.locator('#chat-input').press('Enter')
await page.locator('.turnmeta').nth(2).waitFor({ timeout: 15000 })
const turn3 = page.locator('.turn').nth(5)
await turn3.locator('button[aria-expanded]').click()
check((await turn3.locator('.think').innerText()) === 'The user said "hi" again.',
  `a bare </think> puts what came before it in the thinking block (${JSON.stringify(await turn3.locator('.think').innerText())})`)
check((await turn3.locator('.md').innerText()).trim() === "Hi! How's it going?",
  `and only the reply in the answer (${JSON.stringify((await turn3.locator('.md').innerText()).trim())})`)
const meta3 = await turn3.locator('.turnmeta').innerText()
check(meta3.includes('arrived inline') && meta3.includes('--reasoning-parser qwen3') && meta3.includes('think 3 (est.) tok'),
  `its meta credits the reasoning to the browser split (${JSON.stringify(meta3.split('\n')[1])})`)

// ── Turn 4: a level. No parser on this deployment, so no token cap ─────────
await chips.filter({ hasText: /^high$/ }).click()
await page.locator('#chat-input').fill('think hard')
await page.locator('#chat-input').press('Enter')
await page.locator('.turnmeta').nth(3).waitFor({ timeout: 15000 })
check(bodies[3]?.reasoning_effort === 'high' && bodies[3]?.chat_template_kwargs?.enable_thinking === true && !('thinking_token_budget' in bodies[3]),
  `a level sends thinking on and reasoning_effort, and no cap vLLM would refuse without a parser (${JSON.stringify({ ...bodies[3], messages: undefined })})`)

// ── Turn 5: a provider model gets the same controls ─────────────────────────
await page.locator('.chatlist button', { hasText: CLOUD }).click()
await page.waitForFunction((name) => document.querySelector('.chathead')?.textContent?.includes(name), CLOUD)
check(JSON.stringify(await chips.allInnerTexts()) === JSON.stringify(['default', 'off', 'on', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']),
  'a provider model is offered off, on and the levels too')
const cloudLine = (await page.locator('.chattop').innerText()).split('\n').find((l) => l.startsWith('Thinking:')) ?? ''
check(cloudLine.startsWith('Thinking: through a provider'), `the head says what a provider row sends (${JSON.stringify(cloudLine)})`)
check((await chips.filter({ hasText: /^default$/ }).getAttribute('aria-pressed')) === 'true', 'the choice is per model: the provider row starts on default')
await chips.filter({ hasText: /^high$/ }).click()
await page.locator('#chat-input').fill('plan it')
await page.locator('#chat-input').press('Enter')
await page.locator('.turnmeta').nth(4).waitFor({ timeout: 15000 })
check(bodies[4]?.model === CLOUD && bodies[4]?.reasoning_effort === 'high' && !('chat_template_kwargs' in bodies[4]) && !('thinking_token_budget' in bodies[4]),
  `a provider gets reasoning_effort and nothing a strict upstream would refuse (${JSON.stringify({ ...bodies[4], messages: undefined })})`)
const cloudTurn = page.locator('.turn').last()
await cloudTurn.locator('button[aria-expanded]').click()
check((await cloudTurn.locator('.think').innerText()) === 'Plan the steps.', 'the provider\'s reasoning stream lands in the thinking block')
check((await cloudTurn.locator('.turnmeta').innerText()).includes('returned by the provider as its own stream'), 'and is credited to the provider')

const stats = await page.locator('.chatstats').innerText()
check(stats.includes('tok/s') && stats.includes('context %') && stats.includes('thinking tok'), 'the session strip shows rate, context and thinking tokens')
note(`session strip: ${stats.replace(/\n/g, ' | ')}`)

const misaligned = []
for (const width of [1440, 1024, 390]) {
  await page.setViewportSize({ width, height: 900 })
  for (const f of await page.evaluate(alignmentAudit)) misaligned.push(`@${width} ${f}`)
}
await page.setViewportSize({ width: 1440, height: 900 })
check(misaligned.length === 0, `every row lines up at 1440/1024/390${misaligned.length ? `:\n        ${misaligned.join('\n        ')}` : ''}`)
check(errors.length === 0, `no uncaught page error${errors.length ? `: ${errors.join('; ')}` : ''}`)

mkdirSync(outDir, { recursive: true })
await page.screenshot({ path: join(outDir, 'chat-thinking.png') })
note('wrote screens/chat-thinking.png')

await engine.close()
done()
