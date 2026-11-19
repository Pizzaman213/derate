// requires: browser
// Shared render/a11y contracts plus the real controller over a scripted HTTP
// boundary. No source-text or incidental prose assertions.
import { checkComponent } from '../lib/component.mjs'
import { openGallery } from '../lib/browser.mjs'

await checkComponent('ProviderSignIn', {
  keyboard: async ({ page, check }) => {
    for (const flow of ['chatgpt', 'claude']) {
      const sec = (state) => `[data-component="ProviderSignIn"][data-variant="${flow}"][data-state="${state}"]`
      for (const state of ['start', 'paste', 'error', 'done']) {
        check(await page.locator(`${sec(state)} button`).filter({ hasText: /^Cancel$/ }).count() === 1, `${flow}/${state}: Cancel is available`)
        const links = await page.$$eval(`${sec(state)} a`, (as) => as.map((a) => ({ target: a.target, rel: a.rel, icon: !!a.querySelector('svg') })))
        check(links.every((l) => l.target === '_blank' && l.rel === 'noopener noreferrer' && l.icon), `${flow}/${state}: external links are safe and identified`)
      }
      check(await page.locator(`${sec('start')} input`).count() === 0, `${flow}: no callback field before start`)
      const input = await page.$eval(`${sec('paste')} input`, (i) => ({ type: i.type, ac: i.autocomplete, sc: i.spellcheck, labelled: !!document.querySelector(`label[for="${i.id}"]`) }))
      check(input.type === (flow === 'claude' ? 'password' : 'url') && input.ac === 'off' && !input.sc && input.labelled, `${flow}: callback input has correct secrecy and accessible label`)
      check(await page.locator(`${sec('paste')} a[href^="${flow === 'claude' ? 'https://claude.ai/' : 'https://auth.openai.com/'}"]`).count() === 1, `${flow}: blocked-popup fallback retains its authorization origin`)
      const error = await page.$eval(`${sec('error')} [role="alert"]`, (e) => {
        const p = document.createElement('span')
        p.style.color = 'var(--fault)'
        document.body.append(p)
        const fault = getComputedStyle(p).color
        p.remove()
        return { text: e.textContent, color: getComputedStyle(e).color, ws: getComputedStyle(e).whiteSpace, fault }
      })
      check(!!error.text && error.color === error.fault && error.ws === 'pre-wrap', `${flow}: refusal has alert semantics, fault color and preserved whitespace`)
      check(await page.locator(`${sec('paste')} [role="alert"]`).count() === 0, `${flow}: no fabricated error`)
      check(await page.locator(`${sec('paste')} button`).filter({ hasText: /^Start again$/ }).count() === 1, `${flow}: expired or consumed attempts can restart`)
    }
    for (const state of ['start', 'done']) {
      check(await page.locator(`[data-variant="claude"][data-state="${state}"] a[href="https://code.claude.com/docs/en/legal-and-compliance"]`).count() === 1, `Claude ${state}: official policy is reachable`)
    }
    await checkController(check)
  },
})

async function checkController(check) {
const g = await openGallery({ query: 'only=ProviderSignIn&variant=controller' })
const { page } = g
const controller = '[data-sign-in-controller]'
const action = (name) => page.getByRole('button', { name, exact: true })

// Only the HTTP boundary and popup opening are scripted. The production
// Backend, redactor, state machine, timers and callbacks all execute unchanged.
async function reset(flow = 'chatgpt', provider = 'new', options = {}) {
  await page.goto(`${g.url}/index.html?only=ProviderSignIn&variant=controller`, { waitUntil: 'networkidle' })
  await page.evaluate((options) => {
    const previous = window.fetch
    const state = window.__signIn = { requests: [], popups: [], pending: [], starts: 0, status: 'pending', ...options }
    window.open = (...args) => { state.popups.push(args); return null }
    window.fetch = (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href)
      if (!url.pathname.startsWith('/api/sign-in/')) return previous(input, init)
      const path = url.pathname
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(init.body) : null
      state.requests.push({ path, method, body })
      const response = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
      if (method === 'POST' && !path.endsWith('/complete')) {
        const flow = path.split('/').at(-1)
        const attempt = `${flow}-${++state.starts}`
        const answer = () => response({ attempt_id: attempt, authorize_url: `https://${flow === 'claude' ? 'claude.ai' : 'auth.openai.com'}/oauth/authorize?state=${attempt}`, redirect_uri: 'https://example.invalid/callback', expires_at: Date.now() / 1000 + (state.expiry ?? 600) })
        return state.holdStart ? new Promise((resolve) => state.pending.push(() => resolve(answer()))) : Promise.resolve(answer())
      }
      if (method === 'POST') {
        const answer = () => state.completeError
          ? response({ error: state.completeError }, 400)
          : response({ provider_id: 'connected', kind: path.includes('/claude/') ? 'claude_code' : 'chatgpt' }, 201)
        return state.holdComplete ? new Promise((resolve) => state.pending.push(() => resolve(answer()))) : Promise.resolve(answer())
      }
      const answer = () => state.status === 'unknown'
        ? response({ error: { code: 'sign_in_attempt_unknown', message: 'Expired fixture attempt. Start again.' } }, 404)
        : response(state.status === 'done' ? { status: 'done', provider_id: 'connected' } : { status: 'pending' })
      return state.holdStatus ? new Promise((resolve) => state.pending.push(() => resolve(answer()))) : Promise.resolve(answer())
    }
  }, options)
  await page.selectOption('#fixture-flow', flow)
  await page.selectOption('#fixture-provider', provider)
}

async function begin(flow) {
  await action(`Continue with ${flow === 'claude' ? 'Claude' : 'ChatGPT'}`).click()
  await page.waitForSelector(`${controller} input`)
}

try {
  for (const flow of ['chatgpt', 'claude']) {
    await reset(flow)
    await begin(flow)
    const callback = flow === 'claude' ? 'secret-code#claude-1' : 'http://127.0.0.1:1455/auth/callback?code=secret-code&state=chatgpt-1'
    check(await action('Finish sign-in').isDisabled(), `${flow}: empty callback cannot submit`)
    await page.locator(`${controller} input`).fill(callback)
    check(!(await page.locator(controller).innerText()).includes('secret-code'), `${flow}: callback does not become rendered text`)
    await action('Finish sign-in').click()
    await action('Got it').waitFor()
    const sent = await page.evaluate(() => window.__signIn.requests.find((r) => r.path.endsWith('/complete')))
    check(sent.path === `/api/sign-in/${flow}/${flow}-1/complete` && sent.body.callback_url === callback, `${flow}: exact callback and flow reach the normal API`)
    check(await page.locator(`${controller} input`).count() === 0, `${flow}: success removes callback content`)
    check(await page.locator(controller).getAttribute('data-done') === '0', `${flow}: new provider waits for acknowledgement`)
    const popup = await page.evaluate(() => window.__signIn.popups[0])
    check(popup?.[1] === '_blank' && popup?.[2] === 'noopener,noreferrer', `${flow}: popup uses safe features even when blocked`)
    await action('Got it').click()
    check(await page.locator(controller).getAttribute('data-done') === '1', `${flow}: acknowledgement invokes completion once`)

    await reset(flow, 'existing-a')
    await begin(flow)
    await page.locator(`${controller} input`).fill(callback)
    await action('Finish sign-in').click()
    await action('Open sign-in').waitFor()
    check(await page.locator(controller).getAttribute('data-done') === '1', `${flow}: reconnect closes immediately`)
    check((await page.evaluate(() => window.__signIn.requests[0].body.provider_id)) === 'existing-a', `${flow}: reconnect identifies the provider`)
  }

  await reset('chatgpt', 'new', { status: 'done' })
  await begin('chatgpt')
  await action('Got it').waitFor()
  check((await page.evaluate(() => window.__signIn.requests)).some((r) => r.method === 'GET' && r.path === '/api/sign-in/chatgpt/chatgpt-1'), 'ChatGPT loopback completion advances through its status poll')

  await reset('claude', 'new', { completeError: { code: 'sign_in_attempt_unknown', message: 'Exact upstream refusal.\nStart over without losing this sentence.' } })
  await begin('claude')
  await page.locator(`${controller} input`).fill('secret-code#claude-1')
  await action('Finish sign-in').click()
  await page.getByRole('alert').waitFor()
  check(await page.getByRole('alert').innerText() === 'Exact upstream refusal.\nStart over without losing this sentence.', 'server error renders verbatim, including line breaks')
  await action('Start again').click()
  await page.waitForFunction(() => window.__signIn.starts === 2 && !!document.querySelector('[data-sign-in-controller] input'))
  check(await page.locator(`${controller} input`).inputValue() === '', 'restart discards the previous callback')
  check(await page.getByRole('alert').count() === 0, 'restart clears the old refusal')

  for (const options of [{ expiry: -1 }, { status: 'unknown' }]) {
    await reset('claude', 'new', options)
    await begin('claude')
    await page.getByRole('alert').waitFor()
    check(await action('Start again').isEnabled(), 'expired or missing attempt has a usable restart action')
  }

  // Change flow in place, without relying on the caller to remount it.
  await reset('chatgpt')
  await begin('chatgpt')
  await page.locator(`${controller} input`).fill('old-chatgpt-secret')
  await page.selectOption('#fixture-flow', 'claude')
  await action('Continue with Claude').waitFor()
  await begin('claude')
  check(await page.locator(`${controller} input`).inputValue() === '', 'flow change cannot show the previous callback')
  await page.locator(`${controller} input`).fill('old-claude-secret')
  await page.selectOption('#fixture-flow', 'chatgpt')
  await begin('chatgpt')
  check(await page.locator(`${controller} input`).inputValue() === '', 'reverse flow change also clears callback content')
  await page.locator(`${controller} input`).fill('cancel-secret')
  await action('Cancel').click()
  await action('Open sign-in').click()
  await begin('chatgpt')
  check(await page.locator(`${controller} input`).inputValue() === '', 'cancel and reopen discard callback content')

  // A cancelled start cannot open a late popup or resurrect its form.
  await reset('claude', 'new', { holdStart: true })
  await action('Continue with Claude').click()
  await page.waitForFunction(() => window.__signIn.pending.length === 1)
  await action('Cancel').click()
  await page.evaluate(() => window.__signIn.pending.shift()())
  await page.waitForTimeout(50)
  check(await action('Open sign-in').count() === 1 && await page.evaluate(() => window.__signIn.popups.length) === 0, 'late start after cancel is harmless')

  // A submitted reconnect for A may succeed remotely, but cannot close B.
  await reset('claude', 'existing-a', { holdComplete: true })
  await begin('claude')
  await page.locator(`${controller} input`).fill('secret-a#claude-1')
  await action('Finish sign-in').click()
  await page.waitForFunction(() => window.__signIn.pending.length === 1)
  await page.selectOption('#fixture-provider', 'existing-b')
  await action('Continue with Claude').waitFor()
  await page.evaluate(() => window.__signIn.pending.shift()())
  await page.waitForTimeout(50)
  check(await action('Continue with Claude').count() === 1 && await page.locator(controller).getAttribute('data-done') === '0', 'late reconnect cannot close another provider form')

  // The same isolation applies to status requests already in flight.
  await reset('chatgpt', 'existing-a', { holdStatus: true, status: 'done' })
  await begin('chatgpt')
  await page.waitForFunction(() => window.__signIn.pending.length > 0)
  await page.selectOption('#fixture-flow', 'claude')
  await action('Continue with Claude').waitFor()
  await page.evaluate(() => window.__signIn.pending.splice(0).forEach((resolve) => resolve()))
  await page.waitForTimeout(50)
  check(await action('Continue with Claude').count() === 1 && await page.locator(controller).getAttribute('data-done') === '0', 'late poll from the previous flow cannot invoke completion')
  check(g.errors.length === 0, 'controller cases produce no console error or uncaught exception')
} finally {
  await g.close()
}
}
