// The provider row's key-check line, the environment-key note and the
// warn-days field.
//
// Hermetic. The class of bug here is not a type: a check that never ran
// reading as one that passed, a refusal drawn in a calm colour, an
// environment key whose restart requirement goes unsaid, or a warn-days box
// that sends 1.5 or 366 for the server to refuse.
//
//   cd ui && node src/tabs/settings/keycheck.check.mjs

import { load, report } from '../../check/harness.mjs'

const K = await load(import.meta.url, './keycheck.ts')
const { check, done } = report()

const NOW = 1_800_000_000
const at = (state, secondsAgo, detail = null) => ({ state, checked_at: NOW - secondsAgo, detail })

check(K.keyCheckNote(null, NOW) === 'key not checked yet', 'no check is "not checked yet", never a pass')
check(K.keyCheckNote(at('ok', 30), NOW) === 'key check passed 30s ago', `ok (${K.keyCheckNote(at('ok', 30), NOW)})`)
check(K.keyCheckNote(at('rejected', 30), NOW) === 'key check rejected 30s ago', 'rejected')
check(
  K.keyCheckNote(at('inconclusive', 30, 'could not reach x: ConnectError'), NOW)
    === 'key check inconclusive 30s ago: could not reach x: ConnectError',
  'inconclusive carries its detail',
)

const ago = (s) => K.keyCheckNote(at('ok', s), NOW).replace('key check passed ', '')
check(ago(89) === '89s ago', `89s stays seconds (${ago(89)})`)
check(ago(90) === '2m ago', `90s becomes minutes (${ago(90)})`)
check(ago(5399) === '90m ago', `5399s stays minutes (${ago(5399)})`)
check(ago(5400) === '2h ago', `5400s becomes hours (${ago(5400)})`)
check(ago(172800) === '2d ago', `two days is days (${ago(172800)})`)
check(ago(-5) === '0s ago', 'a clock skew into the future clamps at 0')

check(K.keyCheckTone(at('ok', 1)) === 'ok', 'ok tone')
check(K.keyCheckTone(at('rejected', 1)) === 'fault', 'a refusal is a fault')
check(K.keyCheckTone(at('inconclusive', 1)) === 'muted', 'inconclusive is muted')
check(K.keyCheckTone(null) === 'muted', 'unchecked is muted')

for (const source of ['secrets.json', 'oauth', null]) {
  check(K.environmentKeyNote(source, 'DERATE_OPENROUTER_API_KEY') === null, `no restart note for ${source}`)
}
check(K.environmentKeyNote('environment', '') === null, 'no note without a reference to name')
{
  const note = K.environmentKeyNote('environment', 'DERATE_OPENROUTER_API_KEY') ?? ''
  check(note.startsWith('DERATE_OPENROUTER_API_KEY '), 'the note names the variable')
  check(note.includes('restart'), 'and says a restart is needed')
}

check(K.parseWarnDays('7') === 7, '7')
check(K.parseWarnDays(' 0 ') === 0, '0, trimmed: 0 turns the early warning off')
check(K.parseWarnDays('365') === 365, '365 is the ceiling')
for (const bad of ['366', '-1', '1.5', '', 'abc']) {
  check(K.parseWarnDays(bad) === null, `refused: ${JSON.stringify(bad)}`)
}

done()
