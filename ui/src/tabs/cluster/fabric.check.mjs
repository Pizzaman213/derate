// Hermetic: pure decisions, no coordinator and no browser.
//
// The Fabric settings section reads one pair out of a report that may cover
// every pair, and has four states that must not collapse: a pair with nothing
// to change is NOT the same as a pair whose other end could not be asked, and
// neither is a pair with no fabric at all.
//
//   cd ui && node src/tabs/cluster/fabric.check.mjs

import { load, report } from '../../check/harness.mjs'

const F = await load(import.meta.url, './fabric.ts')

let failures = 0
const check = (ok, what) => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${what}`)
  if (!ok) failures++
}

const finding = (severity, node_id, iface = 'enp1s0f0np0') => ({
  severity, node_id, iface, sentence: `${severity} on ${node_id}`, fix: 'sudo ethtool -G x rx 8192 tx 8192',
})

console.log('--- picking the pair ---')

const rep = {
  links: [
    { a: 'x', b: 'y', findings: [finding('info', 'x'), finding('warning', 'y'), finding('info', 'y'), finding('warning', 'x')] },
    { a: 'x', b: 'z', findings: [] },
  ],
  unreadable: [{ node_id: 'old', reason: 'The node agent on old has no fabric settings route.' }],
  measured_at: 1,
}

const xy = F.fabricView(rep, 'y', 'x')
check(xy.state === 'findings', 'the pair is found whichever way round the rail names it')
check(
  xy.findings.map((f) => f.sentence).join('|') === 'warning on y|warning on x|info on x|info on y',
  'warnings first, and each severity keeps the server order',
)
check(xy.findings.length === 4, 'nothing dropped while sorting')
check(F.fabricView(rep, 'x', 'z').state === 'clean', 'both ends read and nothing to change is clean')
check(F.fabricView(rep, 'x', 'z').findings.length === 0, 'clean has no findings')

console.log('--- the three ways to have no findings ---')

const old = F.fabricView(rep, 'x', 'old')
check(old.state === 'unreadable', 'an end the server could not ask is unreadable, never clean')
check(old.reasons[0] === rep.unreadable[0].reason, "the server's sentence travels verbatim")
check(F.fabricView(rep, 'x', 'gpu').state === 'no-fabric', 'both answered and no link listed is no-fabric')
check(F.fabricView(rep, 'x', 'gpu').reasons.length === 0, 'another pair\'s unreadable node is not this pair\'s reason')
check(F.fabricView(null, 'x', 'y').state === 'no-fabric', 'no report yet renders nothing alarming')

console.log('--- the summary line ---')

check(F.fabricSummary(xy) === '2 warnings · 2 to tune', 'counts by severity')
check(
  F.fabricSummary(F.fabricView({ ...rep, links: [{ a: 'x', b: 'y', findings: [finding('warning', 'x')] }] }, 'x', 'y')) ===
    '1 warning',
  'singular, and no empty "0 to tune"',
)
check(F.fabricSummary(F.fabricView(rep, 'x', 'z')) === null, 'nothing to count is null, not "0 warnings"')

console.log('--- rail reach ---')

const reach = [
  { node_id: 'x', iface: 'enp1s0f0np0', target: '10.100.152.1', ok: true, ms: 0.123, error: null },
  { node_id: 'x', iface: 'enp1s0f0np0', target: 'fe80::1%enp1s0f0np0', ok: false, ms: 0.2,
    error: 'connection refused: nothing is listening on port 47101' },
  { node_id: 'x', iface: 'enP2p1s0f0np0', target: '10.100.153.1', ok: false, ms: null, error: 'no answer in 1.5 s' },
]
const withReach = { ...rep, links: [{ a: 'x', b: 'y', findings: [], reach }] }
const rv = F.fabricView(withReach, 'y', 'x')
check(rv.reach.length === 3 && rv.reach.map((r) => r.target).join('|') === reach.map((r) => r.target).join('|'),
  'every reach row of the pair arrives, in the server order')
check(F.fabricView(rep, 'x', 'y').reach.length === 0, 'a coordinator from before the probe sends no reach: empty, not a crash')
check(F.fabricView(withReach, 'x', 'old').reach.length === 0, 'an unreadable pair has no reach rows')
check(F.fabricView(null, 'x', 'y').reach.length === 0, 'no report has no reach rows')
const [up, refused, silent] = rv.reach.map(F.reachLine)
check(up.ok && up.ms === '0.12 ms' && up.error === null, 'a connected rail shows its round trip under a millisecond to two places')
check(!refused.ok && refused.error === reach[1].error && refused.ms === '0.20 ms',
  "a refusal keeps the server's error verbatim AND its round trip: the far kernel answered over that rail")
check(!silent.ok && silent.error === 'no answer in 1.5 s' && silent.ms === null, 'silence has the error and no time')
check(F.reachMs(12.345) === '12.3 ms' && F.reachMs(null) === null, 'one place above a millisecond; null stays null')
check(
  F.reachLine({ node_id: 'x', iface: 'i', target: 't', ok: false, ms: null, error: null }).error === 'no answer',
  'a failed row with no error text still says something',
)

report('cluster fabric settings', failures)
