// The session cookie check/auth.mjs lifts off a login answer. Over HTTPS the
// gateway names it `__Host-derate_session` (control_plane/auth cookie prefix);
// a regex that only knew `derate_session` matched inside it and handed the
// browser a cookie under the wrong name, so every /api call went out
// unauthenticated. Hermetic: the login is answered by a stubbed fetch.
import { report } from './harness.mjs'

let setCookie = ''
globalThis.fetch = async () =>
  new Response('{}', { status: 200, headers: { 'set-cookie': setCookie } })

const { login } = await import('./auth.mjs')
const { check, done } = report()

for (const name of ['derate_session', '__Host-derate_session']) {
  setCookie = `${name}=abc123; HttpOnly; Path=/; SameSite=Strict`
  const got = await login('http://coord.test', 'admin', 'pw')
  check(got?.name === name, `${name}: name kept (${JSON.stringify(got)})`)
  check(got?.value === 'abc123', `${name}: value lifted (${JSON.stringify(got)})`)
}

done()
