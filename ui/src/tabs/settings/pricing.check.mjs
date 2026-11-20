// Local model pricing rows and the admin password rule.
//
// Hermetic. The class of bug here is not a type: a blank rate sent as 0 (which
// claims a request cost nothing, rather than that nobody priced it), a price
// for a stopped deployment vanishing from the card and so becoming impossible
// to clear, or a half-priced model reading as priced.
//
//   cd ui && node src/tabs/settings/pricing.check.mjs

import { load, report } from '../../check/harness.mjs'

const P = await load(import.meta.url, './pricing.ts')
const A = await load(import.meta.url, './accounts.ts')
const { check, done } = report()

const model = (id, kinds) => ({ id, context_length: null, target_count: kinds.length, target_kinds: kinds })
const price = (m, i, o) => ({ model: m, input_usd_per_mtok: i, output_usd_per_mtok: o, set_by: 'a', set_at: 1 })

{
  const rows = P.pricingRows(
    [model('b-local', ['local']), model('cloud', ['remote']), model('a-both', ['remote', 'local'])],
    [price('a-both', 1, 2), price('gone', 3, null)],
  )
  check(rows.map((r) => r.model).join(',') === 'a-both,b-local,gone', `local + priced, sorted (${rows.map((r) => r.model)})`)
  check(!rows.some((r) => r.model === 'cloud'), 'a remote-only model is not listed')
  check(rows[0].served && rows[0].price?.input_usd_per_mtok === 1, 'a served model carries its price')
  check(rows[1].price === null, 'a served model with no price row has none')
  check(!rows[2].served && rows[2].price != null, 'a priced model no longer served is still listed')
}
check(P.pricingRows(null, null).length === 0, 'nothing loaded -> no rows')

check(JSON.stringify(P.parseRate('')) === '{"value":null}', 'blank is null, never 0')
check(JSON.stringify(P.parseRate('  ')) === '{"value":null}', 'whitespace is null')
check(P.parseRate('0').value === 0, 'an explicit 0 is 0')
check(P.parseRate('1.25').value === 1.25, 'a decimal parses')
check('error' in P.parseRate('-1'), 'negative is refused')
check('error' in P.parseRate('abc'), 'text is refused')
check('error' in P.parseRate('Infinity'), 'infinity is refused')

check(P.rateText(null) === '' && P.rateText(undefined) === '', 'null rate is an empty field')
check(P.rateText(0) === '0', 'a 0 rate is shown as 0')

check(P.isPriced(price('m', 1, 2)), 'both rates -> priced')
check(!P.isPriced(price('m', 1, null)), 'one rate -> unpriced')
check(!P.isPriced(null), 'no row -> unpriced')

check(A.passwordProblem('short', 'short', 12) !== null, 'short password is refused')
check(A.passwordProblem('long-enough-pw', 'long-enough-px', 12) !== null, 'mismatch is refused')
check(A.passwordProblem('long-enough-pw', 'long-enough-pw', 12) === null, 'a long matching password passes')
check(A.passwordProblem('x'.repeat(12), 'x'.repeat(12), 12) === null, 'exactly the minimum passes')

done()
