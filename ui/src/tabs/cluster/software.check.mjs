// Hermetic: pure decisions, no coordinator and no browser.
//
// A node's build and features against the coordinator's. The bugs this guards
// are all ones a type cannot see: telling a node on the SAME build to upgrade
// (the cure for a feature that failed to start is its log, not an installer),
// reading "has not said" as "has nothing" or the reverse, and a warning that
// blocks Save when rolling upgrades must stay allowed.
//
//   cd ui && node src/tabs/cluster/software.check.mjs

import { load, report } from '../../check/harness.mjs'

const S = await load(import.meta.url, './software.ts')
const D = await load(import.meta.url, './doc.ts')

let failures = 0
const check = (ok, what) => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${what}`)
  if (!ok) failures++
}

const KNOWN = ['copy_receiver', 'link_health', 'transfer_capacity']
const ALL = [...KNOWN]

console.log('--- build skew ---')

{
  const sw = S.nodeSoftware('spark-4d38', { healthy: true, build: 'old1', build_skew: true, features: ALL }, 'new2', KNOWN)
  check(sw.skew === 'spark-4d38 runs build old1; this coordinator runs new2.', 'a skewed node names both builds')
  check(sw.gaps.length === 0, 'a skewed node with every feature has nothing missing')
}
{
  const sw = S.nodeSoftware('n', { healthy: true, build: 'old1', build_skew: false, features: ALL }, 'new2', KNOWN)
  check(sw.skew === null, "the server's build_skew decides, never a string comparison here")
}
check(
  S.nodeSoftware('n', { healthy: true, build: null, build_skew: false, features: ALL }, 'new2', KNOWN).skew === null,
  'an unknown build is not a skew',
)
check(
  S.nodeSoftware('n', { healthy: true, build: 'old1', build_skew: true, features: ALL }, null, KNOWN).skew ===
    "n runs build old1, which is not this coordinator's.",
  'a skew still reads when the coordinator cannot name its own build',
)

console.log('--- features: not said vs said-and-missing ---')

{
  const sw = S.nodeSoftware('old', { healthy: true, build: 'old1', build_skew: true, features: null }, 'new2', KNOWN)
  check(sw.features === null, 'an agent that did not say has null features, not an empty list')
  check(
    sw.gaps.length === 1 && sw.gaps[0].endsWith('upgrade this node.'),
    'one "upgrade" sentence for an agent too old to say, not one per feature',
  )
}
{
  const sw = S.nodeSoftware('down', { healthy: false, build: null, features: null }, 'new2', KNOWN)
  check(sw.gaps.length === 0, 'a node that never answered has told us nothing either way')
}
{
  const sw = S.nodeSoftware('none', { healthy: true, build: 'new2', build_skew: false, features: [] }, 'new2', KNOWN)
  check(sw.features !== null && sw.features.every((r) => !r.present), 'said-and-empty lists every feature as absent')
  check(sw.gaps.length === 3, 'each absent feature is named')
}

console.log('--- the cure depends on the build ---')

{
  const sw = S.nodeSoftware('spark-4d38', { healthy: true, build: 'old1', build_skew: true, features: ['link_health'] }, 'new2', KNOWN)
  check(sw.gaps[0] === 'spark-4d38 has no copy receiver: upgrade this node.', 'a missing feature on an older build says upgrade')
  check(sw.gaps.length === 2, 'both missing features named')
}
{
  const sw = S.nodeSoftware('spark-26af', { healthy: true, build: 'new2', build_skew: false, features: ['link_health'] }, 'new2', KNOWN)
  check(sw.gaps.every((g) => !g.includes('upgrade')), 'the same build is never told to upgrade')
  check(sw.gaps[0].includes('failed to start there'), 'the same build is told it failed to start')
}
{
  const sw = S.nodeSoftware('x', { healthy: true, build: null, build_skew: false, features: ['link_health'] }, 'new2', KNOWN)
  check(sw.gaps[0].endsWith('upgrade this node.'), 'an unknown build is not agreement: upgrade')
}

console.log('--- the compact list ---')

{
  const sw = S.nodeSoftware('n', { healthy: true, build: 'b', build_skew: false, features: ['link_health', 'turbo_mode'] }, 'b', KNOWN)
  check(
    S.featureLine(sw.features) === 'no copy receiver · link health · no transfer capacity · turbo mode',
    'known features in order, missing ones marked, a newer node\'s extra feature kept',
  )
}
check(
  S.nodeSoftware('n', { healthy: true, build: 'a', features: null }, 'b', []).gaps.length === 0,
  'a gateway that knows no features (older than the field) calls nothing missing',
)

console.log('--- on the Cluster screen: warns, never blocks Save ---')

{
  const node = (id, extra) => ({
    profile: { node_id: id },
    healthy: true,
    state: 'healthy',
    role: 'worker',
    ...extra,
  })
  const s = {
    topology: { nodes: [{ node_id: 'a' }, { node_id: 'b', label: 'Bee' }], edges: [] },
    cluster: {
      summary: { coordinator_build: 'new2', agent_features: KNOWN },
      nodes: [
        node('a', { build: 'new2', build_skew: false, features: ALL }),
        node('b', { build: 'old1', build_skew: true, features: null }),
      ],
      links: [],
      deployments: [],
    },
    routing: [],
    providers: [],
  }
  const doc = { models: [], limits: [], providers: {}, relaunches: {} }
  const issues = D.validate(doc, s, {})
  const mine = issues.filter((i) => i.id.startsWith('software:'))
  check(mine.length === 2, `the skewed node gets its skew line and its gap (${mine.length})`)
  check(mine.every((i) => i.level === 'warn' && i.live === true), 'every one is a live warning, never an error')
  check(mine.every((i) => i.block === 'machine:b'), 'they focus the skewed machine')
  check(mine[0].message === 'Bee runs build old1; this coordinator runs new2.', 'the machine is named the way the canvas names it')
  check(
    issues.filter((i) => i.level === 'error' && !i.live).length === 0,
    'nothing here counts toward the blocking total',
  )
}

report('cluster node software', failures)
