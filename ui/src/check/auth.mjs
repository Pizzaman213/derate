// The gateway answers /api only to an admin session and /v1 only to a `derate-`
// API key (control_plane/gateway/authz.py). Every verifier that talks to a
// live coordinator imports this file for its side effect: it logs in once as
// $DERATE_CHECK_ADMIN_USER and wraps `globalThis.fetch` so a request to
// $DERATE_CHECK_ORIGIN carries the session cookie on /api and
// `Authorization: Bearer $DERATE_CHECK_API_KEY` on /v1. Nothing else changes
// in the verifier -- its fetches are the same calls the UI makes, through the
// same auth the UI goes through.
//
// Unset credentials are not a skip. The wrapper still installs, every gated
// call answers 401, and the verifier fails saying so -- which is the point:
// "checked nothing" must not look like "checked everything".
//
//   import '../../check/auth.mjs'                  // side effect, before any fetch
//   import { sessionCookie } from '../../check/auth.mjs'   // for a browser context

export const ORIGIN = process.env.DERATE_CHECK_ORIGIN ?? 'http://localhost:8088'
const USER = process.env.DERATE_CHECK_ADMIN_USER ?? ''
const PASSWORD = process.env.DERATE_CHECK_ADMIN_PASSWORD ?? ''
export const API_KEY = process.env.DERATE_CHECK_API_KEY ?? ''

/** The coordinator's own fetch, unwrapped. For a verifier that has to see
 *  what an unauthenticated caller sees. */
export const rawFetch = globalThis.fetch.bind(globalThis)

/** `POST /api/auth/login` -> the `derate_session=<value>` pair, or null when
 *  no credentials are configured. Throws with the gateway's own message when
 *  the login is refused, so a wrong password reads as one. */
export async function login(origin = ORIGIN, username = USER, password = PASSWORD) {
  if (!username) return null
  const res = await rawFetch(`${origin}/api/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    throw new Error(`login as ${username} on ${origin} answered ${res.status}: ${await res.text()}`)
  }
  const set = res.headers.get('set-cookie') ?? ''
  const m = set.match(/((?:__Host-)?derate_session)=([^;]+)/)
  if (!m) throw new Error(`login on ${origin} set no derate_session cookie`)
  return { name: m[1], value: m[2] }
}

let session = null
try {
  session = await login()
} catch (e) {
  // Reported, not thrown: an unreachable coordinator is the verifier's own
  // requirement to fail on, with its own message.
  console.error(`check/auth: ${e.message}`)
}

/** The session as a browser cookie, for `context.addCookies`. Null when no
 *  admin is configured. */
export const sessionCookie = session ? { ...session, url: ORIGIN } : null

globalThis.fetch = (input, init = {}) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.startsWith(ORIGIN)) return rawFetch(input, init)
  const path = new URL(url).pathname
  const headers = new Headers(init.headers ?? (typeof input === 'object' && 'headers' in input ? input.headers : undefined))
  if (path.startsWith('/api') && session && !headers.has('cookie')) {
    headers.set('cookie', `${session.name}=${session.value}`)
  }
  if (path.startsWith('/v1') && API_KEY && !headers.has('authorization')) {
    headers.set('authorization', `Bearer ${API_KEY}`)
  }
  return rawFetch(input, { ...init, headers })
}
