// Hermetic: pure decisions, no coordinator and no browser.
//
// What Settings -> Storage's "Models by node" table says each model is on each
// node, and when a Copy to <node> button is disabled before anybody clicks it.
//
//   cd ui && node src/tabs/storage/presence.check.mjs

import { load, report } from '../../check/harness.mjs'

const P = await load(import.meta.url, './presence.ts')
const { check, done } = report()

const GiB = 1024 ** 3
const repo = (folder, bytes) => ({
  folder,
  repo_id: folder.replace(/^models--/, '').replace('--', '/'),
  bytes,
  blob_count: 1,
  revisions: ['main'],
  last_modified: null,
})
const node = (node_id, repos, extra = {}) => ({
  node_id,
  filesystems: [],
  estate: [],
  unreadable: [],
  available: true,
  reason: null,
  models: { available: true, path: '/c', repos, total_bytes: null, reason: null },
  ...extra,
})

const BIG = 'models--Qwen--Qwen3-30B-A3B'
const SMALL = 'models--Qwen--Qwen3-4B'
const HUGE = 'models--deepseek-ai--DeepSeek-V3'

const coord = node('spark-26af', [repo(BIG, 58 * GiB), repo(SMALL, 8 * GiB), repo(HUGE, 640 * GiB)])
const peer = node('spark-4d38', [repo(SMALL, 8 * GiB), repo(BIG, 3 * GiB)])
const box = node('box-3090', [])

const replication = {
  enabled: true,
  floor_bytes: 0,
  checked_at: 1,
  proposals: [
    { folder: SMALL, repo_id: 'Qwen/Qwen3-4B', node_id: 'box-3090', bytes: 8 * GiB, reasons: [], evict: [], free_bytes: null, status: 'awaiting_approval', transfer_id: null, last_error: null },
  ],
  skipped: [
    { folder: HUGE, repo_id: 'deepseek-ai/DeepSeek-V3', node_id: 'box-3090', sentence: "deepseek-ai/DeepSeek-V3's weights (640 GiB) are more than box-3090's 24 GiB of memory, so it could never be served there." },
    // Not a property of the pair: no room NOW. Must not read as won't fit.
    { folder: HUGE, repo_id: 'deepseek-ai/DeepSeek-V3', node_id: 'spark-4d38', sentence: 'spark-4d38 has 314 GiB free and keeps 200 GiB in reserve.' },
  ],
  replicas: [
    { folder: BIG, repo_id: 'Qwen/Qwen3-30B-A3B', node_id: 'spark-4d38', bytes: 58 * GiB, placed_at: 1, used_at: null, complete: false },
    { folder: SMALL, repo_id: 'Qwen/Qwen3-4B', node_id: 'spark-4d38', bytes: 8 * GiB, placed_at: 1, used_at: null, complete: true },
  ],
}

console.log('--- one row per model, one column per node ---')
const t = P.presenceTable([peer, box, coord], replication, [], 'spark-26af')
check(JSON.stringify(t.nodes) === JSON.stringify(['spark-26af', 'box-3090', 'spark-4d38']),
  'the coordinator is the first column and the rest are sorted')
check(t.rows.length === 3, 'every folder any cache lists is a row, once')
check(t.rows.map((r) => r.folder).join() === [HUGE, BIG, SMALL].join(), 'rows are largest first')
const at = (table, folder, n) => table.rows.find((r) => r.folder === folder).cells[n]

console.log('--- each state ---')
check(at(t, SMALL, 'spark-26af').state === 'present' && at(t, SMALL, 'spark-26af').bytes === 8 * GiB,
  'a cached model is present, with its size')
check(at(t, SMALL, 'spark-4d38').state === 'present', 'a COMPLETE derate copy is simply present')
check(at(t, BIG, 'spark-4d38').state === 'partial',
  'an incomplete derate copy is partial even though the cache lists the folder')
check(at(t, BIG, 'spark-4d38').bytes === 3 * GiB, '...and still says how much of it is on disk')
check(at(t, SMALL, 'box-3090').state === 'proposed', 'a proposal awaiting approval is proposed')
check(at(t, HUGE, 'box-3090').state === 'wontfit', "a 'could never be served' skip is won't fit")
check(at(t, HUGE, 'box-3090').note.includes('could never be served'), "...carrying the server's sentence")
check(at(t, HUGE, 'spark-4d38').state === 'absent', 'a skip for want of room NOW is not won\'t fit')
check(at(t, BIG, 'box-3090').state === 'absent', 'nothing known is absent')

console.log('--- copying outranks what the disk and the record say ---')
const running = [{ transfer_id: 'tr-1', folder: BIG, repo_id: 'Qwen/Qwen3-30B-A3B', node_id: 'spark-4d38', streams: 8, path: 'fabric', routes: [], link: null, total: 100, completed: 25, status: 'running', error: null, started_at: 1, finished_at: null, rate_bps: null, eta_s: null }]
const c = P.presenceTable([coord, peer, box], replication, running, 'spark-26af')
check(at(c, BIG, 'spark-4d38').state === 'copying', 'a running transfer is copying, not partial')
check(at(c, BIG, 'spark-4d38').fraction === 0.25, '...with how far it is')
const verifying = [{ ...running[0], status: 'verifying', completed: 100 }]
check(at(P.presenceTable([coord, peer], replication, verifying), BIG, 'spark-4d38').state === 'copying',
  'verifying is still copying')
for (const status of ['done', 'error']) {
  const finished = [{ ...running[0], status }]
  check(at(P.presenceTable([coord, peer], replication, finished), BIG, 'spark-4d38').state === 'partial',
    `a ${status} transfer is no longer copying`)
}
const noTotal = [{ ...running[0], total: null }]
check(at(P.presenceTable([coord, peer], replication, noTotal), BIG, 'spark-4d38').fraction === null,
  'no total, no fraction -- never a guess')
const approvedCopying = {
  ...replication,
  proposals: [{ ...replication.proposals[0], status: 'copying', transfer_id: 'tr-9' }],
}
check(at(P.presenceTable([coord, peer, box], approvedCopying, [], 'spark-26af'), SMALL, 'box-3090').state === 'copying',
  "a proposal whose status is 'copying' is copying")

console.log('--- a node that could not be read never claims absence ---')
const dark = node('spark-4d38', [], {
  models: { available: false, path: null, repos: [], total_bytes: null, reason: 'The node agent did not answer.' },
})
const u = P.presenceTable([coord, dark], null, [], 'spark-26af')
check(at(u, SMALL, 'spark-4d38').state === 'unknown', 'unreadable cache is unknown, not absent')
check(at(u, SMALL, 'spark-4d38').note === 'The node agent did not answer.', "...and says the server's reason")
check(P.presenceTable([], null, []).rows.length === 0, 'no storage, no rows')

console.log('--- an older revision is stale, not present ---')
const NEW = 'c'.repeat(40)
const OLD = 'b'.repeat(40)
const at4b = (rev, extra = {}) => ({ ...repo(SMALL, 8 * GiB), ...rev, ...extra })
const coordNew = node('spark-26af', [at4b({ revisions: [NEW], commit: NEW })])
const peerOld = node('spark-4d38', [at4b({ revisions: [OLD], commit: OLD })])
const peerBoth = node('spark-4d38', [at4b({ revisions: [OLD, NEW], commit: NEW })])
const s = P.presenceTable([coordNew, peerOld], null, [], 'spark-26af')
check(at(s, SMALL, 'spark-4d38').state === 'stale', "a node lacking the coordinator's refs/main commit is stale")
check(at(s, SMALL, 'spark-4d38').note === P.STALE_NOTE && P.STALE_NOTE === 'older revision', "...saying 'older revision'")
check(at(s, SMALL, 'spark-4d38').bytes === 8 * GiB, '...and still how much is on disk')
check(at(s, SMALL, 'spark-26af').state === 'present', 'the coordinator is never stale against itself')
check(at(P.presenceTable([coordNew, peerBoth], null, [], 'spark-26af'), SMALL, 'spark-4d38').state === 'present',
  'a node holding the commit among others is present')
const recordOld = { enabled: true, floor_bytes: 0, checked_at: null, proposals: [], skipped: [],
  replicas: [{ folder: SMALL, repo_id: 'Qwen/Qwen3-4B', node_id: 'spark-4d38', bytes: 8 * GiB, placed_at: 1, used_at: null, complete: true, revision: OLD }] }
check(at(P.presenceTable([coordNew, peerBoth], recordOld, [], 'spark-26af'), SMALL, 'spark-4d38').state === 'stale',
  "a derate replica recorded at another commit is stale")
const recordNew = { ...recordOld, replicas: [{ ...recordOld.replicas[0], revision: NEW }] }
check(at(P.presenceTable([coordNew, peerBoth], recordNew, [], 'spark-26af'), SMALL, 'spark-4d38').state === 'present',
  "...and present when the record names the coordinator's commit")
check(at(P.presenceTable([node('spark-26af', [repo(SMALL, 8 * GiB)]), peerOld], recordOld, [], 'spark-26af'), SMALL, 'spark-4d38').state === 'present',
  'no commit on the coordinator (an older agent, no refs/main) proves nothing: present')
const copyingStale = [{ ...running[0], folder: SMALL }]
check(at(P.presenceTable([coordNew, peerOld], null, copyingStale, 'spark-26af'), SMALL, 'spark-4d38').state === 'copying',
  'a copy bringing it up to date outranks stale')

console.log('--- Copy to <node>: blocked before the click ---')
const old = node('box-3090', [], {
  copy_receiver: { available: false, reason: 'box-3090 runs an older build without the copy receiver; upgrade it', free: null },
})
const ready = node('spark-4d38', [], { copy_receiver: { available: true, reason: null, free: 300 * GiB } })
check(P.copyBlocked([coord, old, ready], 'box-3090').includes('older build'), 'a node without the receiver is blocked with its reason')
check(P.copyBlocked([coord, old, ready], 'spark-4d38') === null, 'a node whose receiver answered is not blocked')
check(P.copyBlocked([coord, peer], 'spark-4d38') === null, 'an older gateway that did not report blocks nothing')
check(P.copyBlocked([coord], 'nobody') === null, 'a target missing from storage blocks nothing')

done()
