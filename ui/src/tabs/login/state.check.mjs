// requires: coordinator
//
// `GET /api/auth/state` is the one question every load asks before anything
// else (state/auth.tsx), and the sign-in form keys off the codes the login
// route answers. This pins both against a live coordinator:
//
//  - the state has exactly `admin_exists`, `authenticated`, `setup_local_only`,
//    `username` -- a field added or renamed there changes which screen the
//    gate picks;
//  - without a cookie it says authenticated=false, and with the check admin's
//    session it says true and names them;
//  - a wrong password answers 401 with code `invalid_credentials`.
//
// The wrong-password probe uses a throwaway username, never the check admin:
// lockout counts failures per username AND per ip, so it is spent once per run
// and stays well under the threshold (contracts/auth.py LOGIN_LOCK_THRESHOLD).

import '../../check/auth.mjs'
import { ORIGIN, rawFetch, sessionCookie } from '../../check/auth.mjs'
import { report } from '../../check/harness.mjs'

const { check, note, done } = report()

const KEYS = ['admin_exists', 'authenticated', 'setup_local_only', 'username']

const raw = await rawFetch(`${ORIGIN}/api/auth/state`)
check(raw.status === 200, `GET /api/auth/state without a cookie answers 200 (got ${raw.status})`)
const anon = await raw.json()
check(
  JSON.stringify(Object.keys(anon).sort()) === JSON.stringify(KEYS),
  `the state has exactly ${KEYS.join(', ')} (got ${Object.keys(anon).sort().join(', ')})`,
)
check(typeof anon.admin_exists === 'boolean', 'admin_exists is a boolean')
check(anon.authenticated === false, 'without a cookie: authenticated=false')
check(typeof anon.setup_local_only === 'boolean', 'setup_local_only is a boolean')
check(anon.username === null, 'without a cookie: username is null')

if (sessionCookie) {
  const res = await fetch(`${ORIGIN}/api/auth/state`)
  const me = await res.json()
  check(me.authenticated === true, 'with the check admin\'s session: authenticated=true')
  check(me.username === process.env.DERATE_CHECK_ADMIN_USER, `with the session: username is the check admin (got ${me.username})`)
  check(me.admin_exists === true, 'with a session: admin_exists=true')
} else {
  check(false, 'DERATE_CHECK_ADMIN_USER/PASSWORD are set (no session to check the signed-in state with)')
}

const throwaway = `state-check-${Date.now().toString(36)}`
const bad = await rawFetch(`${ORIGIN}/api/auth/login`, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ username: throwaway, password: 'definitely-not-the-password' }),
})
const text = await bad.text()
let code = null
try {
  code = JSON.parse(text)?.error?.code ?? null
} catch {
  // reported below
}
check(bad.status === 401, `a wrong password answers 401 (got ${bad.status})`)
check(code === 'invalid_credentials', `with code invalid_credentials (got ${code}; body ${text.slice(0, 200)})`)
check(!/set-cookie/i.test([...bad.headers.keys()].join(' ')), 'a refused login sets no cookie')
note(`probed as ${throwaway}`)

done()
