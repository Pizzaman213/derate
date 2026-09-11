// Verifier for alerts.ts, the rail's "what is wrong" rows. Same rule as
// activity.check.mjs: there is no UI test runner here, so esbuild-bundle the
// module and exercise it, because a green tsc says nothing about whether the
// rail states an honest fact.
//
// Every rule below compiles perfectly while lying on screen. The one that
// matters most is `since: null` -- a budget that was already over when the
// coordinator started has a day but no minute, and rendering "just now" or
// the process start time there invents a fact the server deliberately
// refused to invent.
//
//   node ui/src/sidebar/alerts.check.mjs
import { build as bundleWithEsbuild } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const out = mkdtempSync(join(tmpdir(), 'alerts-'))
const bundle = join(out, 'bundle.mjs')
await bundleWithEsbuild({
  entryPoints: [join(here, 'alerts.ts')],
  bundle: true,
  format: 'esm',
  outfile: bundle,
  logLevel: 'warning',
})
const { alertRows, sinceLabel, countLabel, kindTitle } = await import(
  pathToFileURL(bundle).href
)

let passed = 0
let failed = 0
const check = (cond, what) => {
  if (cond) passed++
  else {
    failed++
    console.error(`  FAIL ${what}`)
  }
}

const NOW = 1_000_000

const alert = (over = {}) => ({
  key: 'node_down:connor-pi',
  kind: 'node_down',
  severity: 'fault',
  subject: 'connor-pi',
  subject_kind: 'node',
  detail: 'connor-pi stopped answering after 3 consecutive health misses',
  since: NOW - 7200,
  evidence: 'ConnectError',
  count: 1,
  last_seen: NOW,
  day: null,
  ...over,
})

console.log('\n--- a start we do not know is said, never drawn as a time ---')

check(
  sinceLabel(alert({ since: null, day: '2026-09-10' }), NOW).includes('not recorded'),
  'a null start says so',
)
check(
  !/for \d/.test(sinceLabel(alert({ since: null, day: '2026-09-10' }), NOW)),
  'and never renders as an elapsed time',
)
check(
  sinceLabel(alert({ since: null, day: '2026-09-10' }), NOW).includes('2026-09-10'),
  'a budget with no minute still names the day it belongs to',
)
check(
  sinceLabel(alert({ since: null, day: null }), NOW) === 'start not recorded',
  'and with no day either, it just says so',
)

console.log('\n--- elapsed time reads in the right unit ---')
check(sinceLabel(alert({ since: NOW - 30 }), NOW) === 'for 30s', 'seconds')
check(sinceLabel(alert({ since: NOW - 600 }), NOW) === 'for 10m', 'minutes')
check(sinceLabel(alert({ since: NOW - 7200 }), NOW) === 'for 2h', 'hours')
check(sinceLabel(alert({ since: NOW - 3 * 86400 }), NOW) === 'for 3d', 'days')
check(
  sinceLabel(alert({ since: NOW + 500 }), NOW) === 'for 0s',
  'a clock skew into the future never produces a negative age',
)

console.log('\n--- evidence is a program\'s own words, or nothing at all ---')
{
  const [row] = alertRows({ alerts: [alert({ evidence: '' })] }, NOW)
  check(row.evidence === null, 'an empty string becomes null, not an empty block')
}
{
  const [row] = alertRows({ alerts: [alert({ evidence: 'ConnectError' })] }, NOW)
  check(row.evidence === 'ConnectError', 'and real words are carried through unchanged')
  check(!row.detail.includes('ConnectError'), 'the sentence and the evidence stay separate')
}

console.log('\n--- a crash loop is one row that says how many ---')
{
  const [row] = alertRows({ alerts: [alert({ count: 120 })] }, NOW)
  check(countLabel(row) === 'seen 120 times', 'a repeated condition says its count')
}
{
  const [row] = alertRows({ alerts: [alert({ count: 1 })] }, NOW)
  check(countLabel(row) === null, '"seen once" is noise and is not said')
}

console.log('\n--- the operator\'s own name for a machine wins ---')
{
  const [row] = alertRows(
    { alerts: [alert({ subject_label: 'the pi in the cupboard' })] },
    NOW,
  )
  check(row.title.startsWith('the pi in the cupboard'), 'a label is preferred to the id')
}
{
  const [row] = alertRows({ alerts: [alert()] }, NOW)
  check(row.title.startsWith('connor-pi'), 'and the id is used when there is no label')
}

console.log('\n--- nothing wrong draws nothing ---')
check(alertRows(null, NOW).length === 0, 'no report is no rows')
check(alertRows({ alerts: [] }, NOW).length === 0, 'an empty set is no rows')
check(alertRows({}, NOW).length === 0, 'a malformed report is no rows, not a throw')

console.log('\n--- every kind has a heading ---')
for (const kind of ['node_down', 'cap_reached', 'oom']) {
  check(typeof kindTitle(kind) === 'string' && kindTitle(kind).length > 0, `${kind} is titled`)
}

console.log(`\nalerts.check\n  ${passed}/${passed + failed} checks passed`)
process.exit(failed === 0 ? 0 : 1)
