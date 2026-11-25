// requires: browser python -- drives .omp/stubui/server.mjs over MCP stdio
//
// The stub-UI MCP tool (.omp/stubui/server.mjs) end to end, as an agent sees
// it: raw JSON-RPC over the server's stdin/stdout, so this needs none of the
// server's own dependencies to speak to it.
//
// What it pins, in order: every tool says it is the stub; the first call
// starts the stub signed in (a /dashboard that stays /dashboard, not /login,
// and renders for the stub admin); a screenshot is a PNG; the page carries the
// STUB UI badge; the model list renders the fixtures; a write is refused and
// named by `console`; and after `kill -9` of the server its Python child is
// gone and the next server sweeps the dir it left.
//
//   node src/shell/stubui.check.mjs
//
// Needs the server's own dependencies (`npm --prefix .omp install`), ui's
// (`npm ci`: playwright-core, esbuild) and a built ui/dist (`npm run build`);
// without them it fails, naming the server's exit.

import { spawn } from 'node:child_process'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createInterface } from 'node:readline'
import { report } from '../check/harness.mjs'
import { READ_POSTS } from '../../../.omp/stubui/policy.mjs'

const SERVER = fileURLToPath(new URL('../../../.omp/stubui/server.mjs', import.meta.url))
const CLIENT = fileURLToPath(new URL('../api/client.ts', import.meta.url))
const { check: assert, note, done } = report()
/** Label first, and what was seen when it fails. */
const check = (what, ok, seen) => assert(ok, what) || (seen !== undefined && note(String(seen).slice(0, 600)))

/** One MCP server process and a call() that waits for the matching id. If
 *  the server dies, every pending and later call settles as an error rather
 *  than leaving node to exit on an unsettled await with no FAIL line. */
function connect() {
  const proc = spawn('node', [SERVER], { stdio: ['pipe', 'pipe', 'inherit'] })
  const waiting = new Map()
  let exited = null
  let id = 0
  createInterface({ input: proc.stdout }).on('line', (line) => {
    const msg = JSON.parse(line)
    waiting.get(msg.id)?.(msg)
  })
  proc.stdin.on('error', () => {})
  proc.once('exit', (code, sig) => {
    exited = { error: { message: `server exited (${code ?? sig})` } }
    for (const settle of waiting.values()) settle(exited)
    waiting.clear()
  })
  const send = (m) => proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', ...m }) + '\n')
  const call = (method, params = {}) =>
    new Promise((resolve) => {
      if (exited) return resolve(exited)
      waiting.set(++id, resolve)
      send({ id, method, params })
    })
  const tool = async (name, args = {}) => {
    const msg = await call('tools/call', { name, arguments: args })
    return msg.result ?? { isError: true, content: [{ type: 'text', text: msg.error?.message ?? 'no result' }] }
  }
  const init = async () => {
    await call('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'stubui.check', version: '0' } })
    send({ method: 'notifications/initialized' })
  }
  return { proc, call, tool, init }
}

const HEADER = '[STUB UI · fixture data · look only]'
const texts = (r) => r.content.filter((c) => c.type === 'text').map((c) => c.text).join('\n')
/** What the page said, without the header every result opens with. */
const page = (r) => texts(r).split('\n').filter((l) => !l.startsWith(HEADER)).join('\n')
/** A server's dir carries its pid in the name: derate-stubui-<pid>-XXXXXX, under $TMPDIR or /tmp. */
const rootOf = (pid) =>
  [...new Set([tmpdir(), '/tmp'])].flatMap((b) => readdirSync(b).filter((d) => d.startsWith(`derate-stubui-${pid}-`)).map((d) => join(b, d)))[0]
const processesUnder = (root) =>
  readdirSync('/proc').filter((p) => /^\d+$/.test(p)).filter((p) => {
    try {
      return readFileSync(`/proc/${p}/cmdline`, 'utf8').includes(root)
    } catch {
      return false
    }
  })

// Every plan POST the UI sends is a read the look-only browser lets through;
// a new one would otherwise only show up as "blocked POST" in `console`.
const planPosts = [...readFileSync(CLIENT, 'utf8').matchAll(/'(\/api\/plan[^'?]*)'/g)].map((m) => m[1])
check('every /api/plan POST in client.ts is in READ_POSTS', planPosts.length > 0 && planPosts.every((p) => READ_POSTS.has(p)), planPosts)
check('READ_POSTS names no path client.ts no longer sends', [...READ_POSTS].every((p) => planPosts.includes(p)), [...READ_POSTS])

const a = connect()
await a.init()
const tools = (await a.call('tools/list')).result?.tools ?? []
check('the server answered tools/list', tools.length > 0)
check('the server offers open, screenshot, snapshot, click, console', ['open', 'screenshot', 'snapshot', 'click', 'console'].every((n) => tools.some((t) => t.name === n)))
check('every tool description says it is the stub UI', tools.length > 0 && tools.every((t) => t.description.includes('STUB UI')))

const listed = await a.tool('routes')
check('routes answers headed as the stub, without starting it', texts(listed).startsWith(HEADER) && !rootOf(a.proc.pid), texts(listed))

const opened = await a.tool('open', { path: '/dashboard' })
check('the first call starts the stub signed in (/dashboard, not /login)', texts(opened).startsWith(`${HEADER} at /dashboard ·`), texts(opened))
check('every result names the build it shows', texts(opened).includes('build: ui/dist'), texts(opened))

const shot = await a.tool('screenshot')
const png = shot.content.find((c) => c.type === 'image')
check('a screenshot is a PNG image', png?.mimeType === 'image/png' && Buffer.from(png.data, 'base64').subarray(1, 4).toString() === 'PNG')

const snap = page(await a.tool('snapshot'))
check('the dashboard rendered for the stub admin', snap.includes('Sign out agent'), snap)
check('the page carries the STUB UI badge', snap.includes('STUB UI · fixture data'))

// The model list renders over the stub's fixtures. A model's own page does
// not: the stub answers /api/models/detail with 501 and the page dies on it,
// so the plan POSTs READ_POSTS lets through cannot be exercised here.
await a.tool('open', { path: '/models' })
const list = page(await a.tool('snapshot'))
check('the model list renders the stub fixtures', list.includes('Llama-3.3-70B-Instruct'), list)

await a.tool('open', { path: '/dashboard' })
await a.tool('click', { role: 'button', name: 'Sign out' })
const blocked = texts(await a.tool('console'))
check('a write (sign out) is refused and named by console', blocked.includes('blocked POST /api/auth/logout'), blocked)

const root = rootOf(a.proc.pid)
check('the stub and its browser run under the server\u2019s own temp dir', !!root && processesUnder(root).length >= 2, root)
// Checked by place, not by counting /tmp: peers run Playwright here too.
check('the browser profile lives in the server\u2019s dir, where the sweep reaches it', !!root && existsSync(join(root, 'profile')))
a.proc.kill('SIGKILL')
await new Promise((r) => setTimeout(r, 2000))
check('kill -9 of the server takes the stub and the browser with it', !!root && processesUnder(root).length === 0, root && processesUnder(root))

const b = connect()
await b.init()
await b.tool('open', { path: '/cluster' })
check('the next server sweeps the dir a killed one left', !!root && !existsSync(root))
const rootB = rootOf(b.proc.pid)
b.proc.stdin.end()
await new Promise((r) => b.proc.once('exit', r))
check('a clean exit removes its own dir', !!rootB && !existsSync(rootB))

done()
