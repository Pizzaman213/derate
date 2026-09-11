// Hermetic: pure decisions, no coordinator and no browser.
//
// The first verifier to cover anything in the cluster rail. `layout.check.mjs`
// and its three siblings check geometry, motion and loading; the rail's own
// wording and its "which state is this" rules were inline in `SelectionRail.tsx`
// and therefore unreachable by anything -- which is why the rule this file
// exists to hold had no test until the tuning row needed one.
//
//   cd ui && node src/tabs/cluster/tuning.check.mjs

import { load, report } from '../../check/harness.mjs'

const T = await load(import.meta.url, './tuning.ts')

let failures = 0
const check = (ok, what) => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${what}`)
  if (!ok) failures++
}

const DECODE = 8192
const BULK = 4194304

const row = (env, band, us, gbps, error = null) => ({
  size_band: band, microseconds: us, busbw_gbps: gbps, env, error,
})

console.log('--- the four states, and the two that look identical on the wire ---')

check(T.tuningState(null) === 'uncalibrated', 'no tuning object at all is uncalibrated')
check(
  T.tuningState({ calibrated: false, env: {}, rows: [] }) === 'uncalibrated',
  'calibrated:false is uncalibrated',
)

// THE distinction. Both of these carry `env: {}`.
const defaultWon = { calibrated: true, env: {}, rows: [row({}, DECODE, 17.7, 0.3)] }
check(T.tuningState(defaultWon) === 'default-best', 'measured, and the default won, is its OWN state')
check(
  T.tuningState(defaultWon) !== T.tuningState({ calibrated: false, env: {}, rows: [] }),
  'and it is NOT the same state as never having been measured',
)
check(
  T.tuningLabel(defaultWon) !== T.tuningLabel({ calibrated: false, env: {}, rows: [] }),
  'the two read differently on screen, which is the point of telling them apart',
)
check(
  !/not tuned/i.test(T.tuningLabel(defaultWon)),
  'a finished calibration never renders as "not tuned"',
)

const tuned = {
  calibrated: true,
  env: { NCCL_MAX_NCHANNELS: '2' },
  rows: [row({ NCCL_MAX_NCHANNELS: '2' }, DECODE, 17.45, 0.33)],
}
check(T.tuningState(tuned) === 'tuned', 'a setting that won is tuned')
check(T.tuningLabel(tuned) === 'NCCL_MAX_NCHANNELS=2', 'and the label is the setting itself')

console.log('\n--- a fabric that refused is a finding, not a missing measurement ---')
const failed = {
  calibrated: true,
  env: {},
  rows: [row({}, 0, 0, 0, 'IB queue-pair fault')],
}
check(T.tuningState(failed) === 'failed', 'all-errors reads as failed')
check(
  T.tuningState(failed) !== 'uncalibrated',
  'and not as "never tried" -- the difference is the whole finding',
)
check(/did not complete/i.test(T.tuningLabel(failed)), 'the label says so')

console.log('\n--- offering to tune ---')
check(T.canTune(null, false), 'an unmeasured pair can be tuned')
check(T.canTune(defaultWon, false), 'so can one where the default won -- rows can go stale')
check(!T.canTune(tuned, false), 'a tuned pair is not offered again')
check(
  !T.canTune(null, true),
  'and NOTHING is offered while a probe is in flight: calibrate and measure share ' +
    'one server lock, so a second press blocks for minutes rather than refusing',
)

console.log('\n--- the evidence, which is what makes the verdict checkable ---')
const full = {
  calibrated: true,
  env: { NCCL_MAX_NCHANNELS: '2' },
  rows: [
    row({}, DECODE, 17.69, 0.3), row({}, BULK, 1, 8.09),
    row({ NCCL_MAX_NCHANNELS: '2' }, DECODE, 17.45, 0.33),
    row({ NCCL_MAX_NCHANNELS: '2' }, BULK, 1, 17.68),
    row({ NCCL_MAX_NCHANNELS: '4' }, DECODE, 21.87, 0.3),
    row({ NCCL_MAX_NCHANNELS: '4' }, BULK, 1, 18.55),
  ],
}
const ev = T.tuningEvidence(full, DECODE, BULK)
check(ev.length === 3, `one line per candidate (got ${ev.length})`)
check(
  ev[0].label === 'default',
  'the baseline leads, because every other line only means something against it',
)
const four = ev.find((e) => e.label === 'NCCL_MAX_NCHANNELS=4')
const base = ev.find((e) => e.label === 'default')
check(
  four.bulkGbps > base.bulkGbps && four.decodeUs > base.decodeUs,
  'the rejected candidate shows BOTH its win and its cost -- showing only bulk ' +
    'would make the wrong answer look right',
)
check(
  T.tuningEvidence({ calibrated: true, env: {}, rows: [row({}, 0, 0, 0, 'boom')] }, DECODE, BULK)
    .length === 0,
  'an error row contributes no evidence line rather than a row of zeroes',
)
check(T.tuningEvidence(null, DECODE, BULK).length === 0, 'no tuning, no evidence')
check(T.envLabel({}) === 'default', 'the baseline is named, not blank')

report('cluster link tuning', failures)
