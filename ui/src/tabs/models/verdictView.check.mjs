import { load, report } from '../../check/harness.mjs'
const M = await load(import.meta.url, './verdictView.ts')
const { check, done } = report()

const is = (w, label, signal) => w.label === label && w.signal === signal
const base = { running: false, routing: false, providerRuntime: false, variant: null, result: null, checking: false }
const plan = (fit, fit_live = null) => ({ fit, fit_live })
const variant = { verdict: 'fits', static_fits: true, launchable: true, file_bytes: 1, context: 1 }

check(is(M.planWord({ ...base, running: true, routing: true }), 'Running', 'live'), 'running wins over everything')
check(is(M.planWord({ ...base, routing: true }), 'Provider', 'idle'), 'routing reads Provider')
check(is(M.planWord({ ...base, providerRuntime: true, variant }), 'not judged here', 'idle'), 'a provider runtime reads the lamp off the variant')
check(is(M.planWord({ ...base, providerRuntime: true }), 'Not checked', 'idle'), 'a provider runtime with no variant is not checked')
check(is(M.planWord({ ...base, checking: true }), 'Checking…', 'idle'), 'no result while checking')
check(is(M.planWord(base), 'Not checked', 'idle'), 'no result, not checking')
check(is(M.planWord({ ...base, result: plan(null) }), 'Not checked', 'idle'), 'a result with no fit')
check(is(M.planWord({ ...base, result: plan({ verdict: 'wont_fit' }, { verdict: 'fits' }) }), 'Won’t fit', 'fault'), 'static refusal is a fault')
check(is(M.planWord({ ...base, result: plan({ verdict: 'fits' }, { verdict: 'wont_fit' }) }), 'Not now', 'warn'), 'live-only refusal is Not now')
check(is(M.planWord({ ...base, result: plan({ verdict: 'fits' }, { verdict: 'fits_degraded' }) }), 'Slow', 'warn'), 'degraded live is Slow')
check(is(M.planWord({ ...base, result: plan({ verdict: 'fits_degraded' }) }), 'Slow', 'warn'), 'degraded static with no live is Slow')
check(is(M.planWord({ ...base, result: plan({ verdict: 'fits' }, { verdict: 'fits' }) }), 'Fits', 'live'), 'otherwise Fits')
// A host-pool runtime has no static ceiling: its static refusal is against 0
// by design, so the live verdict is the word whenever there is one.
check(is(M.planWord({ ...base, hostPool: true, result: plan({ verdict: 'wont_fit' }, { verdict: 'fits_degraded' }) }), 'Slow', 'warn'), 'host pool: live degraded is Slow, not the static refusal')
check(is(M.planWord({ ...base, hostPool: true, result: plan({ verdict: 'wont_fit' }, { verdict: 'fits' }) }), 'Fits', 'live'), 'host pool: live fits is Fits')
check(is(M.planWord({ ...base, hostPool: true, result: plan({ verdict: 'wont_fit' }, { verdict: 'wont_fit' }) }), 'Not now', 'warn'), 'host pool: live refusal is Not now')
check(is(M.planWord({ ...base, hostPool: true, result: plan({ verdict: 'wont_fit' }) }), 'Won’t fit', 'fault'), 'host pool with no live reading keeps the static refusal')

const BANDS = [
  ['running', false, 'Running', 'live'], ['fits', false, 'Fits', 'live'], ['degraded', false, 'Slow', 'warn'],
  ['notnow', false, 'Not now', 'warn'], ['wont', false, 'Won’t fit', 'fault'], ['unchecked', true, 'Checking…', 'idle'],
  ['unchecked', false, 'Not checked', 'idle'], ['elsewhere', false, 'Provider', 'idle'], ['unserved', false, 'Not served', 'idle'],
]
for (const [b, c, l, s] of BANDS) check(is(M.bandWord(b, c), l, s), `band ${b}${c ? ' (checking)' : ''} reads ${l}`)

const P = [[74e6, '74M'], [0.49e9, '0.5B'], [30.5e9, '30.5B'], [671e9, '671B'], [1.026e12, '1.0T'], [null, '']]
for (const [n, s] of P) check(M.formatParams(n) === s, `formatParams(${n}) is '${s}'`)
check(M.gib(null) === '—', 'gib(null) is a dash')
check(M.gib(2 ** 30 * 5) === '5.0 GiB', 'gib keeps one decimal under 100')
check(M.gib(2 ** 30 * 150) === '150 GiB', 'gib drops decimals at 100 and over')
done()
