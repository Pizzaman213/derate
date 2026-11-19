// The lockout line on the sign-in screen: how a wait is worded, and where the
// wait comes from. Hermetic -- lockout.ts and setup/admin.ts are pure.
//
// The two bugs this exists for: a wait rounded DOWN (61 s shown as "1 min"
// sends the person back while the lock still holds, and the next failure
// doubles it), and the body's `retry_after_s` overriding the `Retry-After`
// header, which is the one HTTP defines and every other client obeys.

import { load, report } from '../../check/harness.mjs'

const L = await load(import.meta.url, './lockout.ts')
const A = await load(import.meta.url, '../setup/admin.ts')
const { check, done } = report()

const edges = [
  [0, 'Try again now'],
  [1, 'Try again in 1 s'],
  [59, 'Try again in 59 s'],
  [60, 'Try again in 1 min'],
  [61, 'Try again in 2 min'],
  [3599, 'Try again in 1 h'],
  [3600, 'Try again in 1 h'],
]
for (const [s, want] of edges) {
  const got = L.formatRetry(s)
  check(got === want, `formatRetry(${s}) = "${got}" (want "${want}")`)
}
check(L.formatRetry(0.2) === 'Try again in 1 s', 'a fractional second rounds up, never to "now"')
check(L.formatRetry(-5) === 'Try again now', 'a lock already past reads "now"')
check(L.formatRetry(3601) === 'Try again in 2 h', '3601 s rounds up to 2 h')

const body = (s) => JSON.stringify({ error: { message: 'locked', code: 'login_locked', retry_after_s: s } })

check(L.retryAfterSeconds('45', body(900)) === 45, 'the Retry-After header wins over the body')
check(L.retryAfterSeconds(null, body(900)) === 900, 'no header: the body\'s retry_after_s')
check(L.retryAfterSeconds('', body(30)) === 30, 'an empty header falls through to the body')
check(L.retryAfterSeconds('soon', body(30)) === 30, 'an unparseable header falls through to the body')
check(L.retryAfterSeconds(null, body(12.2)) === 13, 'a fractional body value rounds up')
check(L.retryAfterSeconds(null, JSON.stringify({ retry_after_s: 7 })) === 7, 'a top-level retry_after_s is read too')
check(L.retryAfterSeconds(null, null) === null, 'nothing said: null, never a guessed default')
check(L.retryAfterSeconds(null, 'not json') === null, 'a non-JSON body: null')
check(L.retryAfterSeconds(null, body(-3)) === null, 'a negative body value is not a wait')

const now = Date.parse('2026-11-17T12:00:00Z')
check(
  L.retryAfterSeconds('Tue, 17 Nov 2026 12:02:00 GMT', body(5), now) === 120,
  'an HTTP-date header is measured from now and still wins',
)
check(
  L.retryAfterSeconds('Tue, 17 Nov 2026 11:00:00 GMT', null, now) === 0,
  'an HTTP-date in the past is zero, not negative',
)

check(L.errorCode(body(1)) === 'login_locked', 'errorCode reads error.code')
check(L.errorCode(JSON.stringify({ error: { message: 'x' } })) === null, 'errorCode: no code is null')
check(L.errorCode('<html>') === null, 'errorCode: a non-JSON body is null')

// The first-admin form: only what the browser can know for certain.
const long = 'x'.repeat(12)
const mail = 'a@b.c'
check(A.adminFormProblem('', mail, long, long) !== null, 'admin form: an empty username is refused')
check(A.adminFormProblem('  ', mail, long, long) !== null, 'admin form: a blank username is refused')
check(A.adminFormProblem('root', '', long, long) !== null, 'admin form: an empty email is refused')
check(A.adminFormProblem('root', '   ', long, long) !== null, 'admin form: a blank email is refused')
check(A.adminFormProblem('root', mail, 'x'.repeat(11), 'x'.repeat(11))?.includes('12') === true, 'admin form: 11 characters names the 12 floor')
check(A.adminFormProblem('root', mail, long, long + 'y') !== null, 'admin form: mismatched passwords are refused')
check(A.adminFormProblem('root', mail, long, long) === null, 'admin form: 12 matching characters and an email pass')

done()
