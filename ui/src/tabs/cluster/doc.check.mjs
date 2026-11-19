// doc.ts: the wiring editor's document, driven without a browser.
//
// The first half reads tests/fixtures/wiring_rules.json -- the file
// tests/unit/test_wiring_rules.py posts to the server -- and asserts that each
// case's edit, made through the editor's own `connect`/`disconnect`, produces
// exactly that case's ops, byte for byte. The server side proves those ops
// get exactly those results, so the two halves pin one contract between them.
//
//   cd ui && node src/tabs/cluster/doc.check.mjs
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { load, report } from '../../check/harness.mjs'

const D = await load(import.meta.url, './doc.ts')
const { check, done } = report()

const fixture = JSON.parse(
  readFileSync(fileURLToPath(new URL('../../../../tests/fixtures/wiring_rules.json', import.meta.url)), 'utf8'),
)
const setup = fixture.setups.default

function apply(doc, steps) {
  for (const step of steps) {
    if (step.connect) doc = D.connect(doc, step.connect.from, step.connect.to)
    else if (step.disconnect) doc = D.disconnect(doc, step.disconnect)
    else if (step.addProviderModel) {
      const a = step.addProviderModel
      doc = D.addProviderModel(doc, a.provider_id, a.upstream_id, a.served_name)
    } else if (step.addLimit) {
      const { id, ...values } = step.addLimit
      doc = D.addLimit(doc, id, values)
    } else if (step.setRoutePolicy) {
      doc = D.setRoutePolicy(doc, step.setRoutePolicy.served_name, step.setRoutePolicy.policy)
    } else if (step.setProviderPin) {
      const p = step.setProviderPin
      doc = D.setProviderPin(doc, p.provider_id, p.upstream_id, p.pin)
    } else if (step.setRouteShares) {
      doc = D.setRouteShares(doc, step.setRouteShares.served_name, step.setRouteShares.shares)
    } else throw new Error(`unknown fixture step ${JSON.stringify(step)}`)
  }
  return doc
}

// ── The shared fixture ───────────────────────────────────────────────────────

let ran = 0
for (const c of fixture.cases) {
  if (c.edit === null) continue
  const s = structuredClone(fixture.setups[c.setup])
  for (const [name, fields] of Object.entries(c.routing ?? {})) Object.assign(s.routing.find((r) => r.served_name === name), fields)
  const base = D.fromServer(s)
  const ops = D.diff(base, apply(base, c.edit), s)
  check(JSON.stringify(ops) === JSON.stringify(c.ops), `fixture "${c.name}": diff() is the case's ops byte for byte`)
  if (JSON.stringify(ops) !== JSON.stringify(c.ops)) console.log('      got', JSON.stringify(ops))
  ran += 1
}
check(ran >= 5, `every edit case in the fixture ran (${ran})`)

// ── fromServer and diff ──────────────────────────────────────────────────────

const base = D.fromServer(setup)
check(JSON.stringify(D.fromServer(setup)) === JSON.stringify(base), 'fromServer is deterministic')
check(D.diff(base, base, setup).length === 0, 'diff(base, base) is empty')
check(base.routes['llama-3.3-70b'].policy === 'auto', 'an auto-selected route reads as auto')
check(
  JSON.stringify(base.providers.openrouter.models) ===
    JSON.stringify({ 'anthropic/claude-sonnet-4.5': 'anthropic/claude-sonnet-4.5', 'openai/gpt-4o-mini': 'openai/gpt-4o-mini' }),
  'a provider entry carries exactly its enabled models',
)

{
  // Limits from the server come back as one block per distinct value set.
  const s = structuredClone(setup)
  for (const r of s.routing) r.limits = r.served_name === 'openai/gpt-4o-mini' ? { tpm: 50 } : { rpm: 3 }
  const doc = D.fromServer(s)
  check(doc.limits.length === 2, 'server limits group into one block per distinct value set')
  check(doc.limits.find((l) => l.rpm === 3)?.plugged.length === 2, 'a shared limit is plugged into every name carrying it')
  check(D.diff(doc, doc, s).length === 0, 'grouped limits round-trip with no ops')
  const unplugged = D.disconnect(doc, `${doc.limits.find((l) => l.tpm === 50).id}|out|split:openai/gpt-4o-mini|limit`)
  const ops = D.diff(doc, unplugged, s)
  check(
    JSON.stringify(ops) === JSON.stringify([{ op: 'route.set', served_name: 'openai/gpt-4o-mini', policy: 'failover', limits: {} }]),
    'unplugging a limit sends {} for that name only',
  )
}

{
  // Op order: provider patches before route limits, whatever the edit order.
  let doc = D.addLimit(base, 'limit:x', { rpm: 5 })
  doc = D.connect(doc, { block: 'limit:x', socket: 'out' }, { block: 'split:llama-3.3-70b', socket: 'limit' })
  doc = D.setProviderEnabled(doc, 'openrouter', false)
  const ops = D.diff(base, doc, setup)
  check(ops.map((o) => o.op).join(',') === 'provider.patch,route.set', 'ops come provider patches first, then route limits')
  check(JSON.stringify(ops[0].patch) === '{"enabled":false}', 'the enabled checkbox is its own patch field')
}

{
  // An explicit override keeps its own policy when its limits change.
  const s = structuredClone(setup)
  const r = s.routing.find((x) => x.served_name === 'llama-3.3-70b')
  r.auto_selected = false
  r.policy = 'round_robin'
  const b = D.fromServer(s)
  let doc = D.addLimit(b, 'limit:y', { tpm: 100 })
  doc = D.connect(doc, { block: 'split:llama-3.3-70b', socket: 'limit' }, { block: 'limit:y', socket: 'out' })
  const op = D.diff(b, doc, s)[0]
  check(op.policy === 'round_robin', 'a sidebar override is never changed silently by a limit edit')
  const split = D.blocks(b, s).find((x) => x.id === 'split:llama-3.3-70b')
  check(split.policyChip === 'policy: round_robin (sidebar)', 'a non-LB policy shows its chip')
  check(split.ports.local === 'local' && split.ports.cloud === 'cloud', 'ports carry no rank under another policy')
}

// ── blocks and wires ─────────────────────────────────────────────────────────

const bs = D.blocks(base, setup)
const ws = D.wires(base, setup)
const machine = (id) => bs.find((b) => b.id === `machine:${id}`)
check(machine('spark-01').rows.length === 1 && machine('spark-02').rows.length === 1, 'a spanning deployment shows a row on each machine')
check(machine('ws-3090').rows.length === 1, 'one row per deployment on its node')
check(machine('spark-01').rows[0].span === 'PP 2', 'the spanning row says how it spans (the fixture plan is PP 2)')
check(machine('spark-01').rows[0].rank === null, 'the lowest node_id carries the wires')
check(
  JSON.stringify(machine('spark-02').rows[0].rank) === JSON.stringify({ k: 2, n: 2 }),
  'the other machine shows rank 2 of 2',
)
check(machine('spark-01').rows[0].title === 'Llama-3.3-70B-Instruct', 'a row is titled by its model, not its deployment id')
const localWires = ws.filter((w) => w.kind === 'local' && w.served === 'llama-3.3-70b')
check(
  localWires.length === 2 && localWires.every((w) => !w.to.block.endsWith('spark-02')),
  'request wires attach to the lowest node_id only',
)
const cloudWires = ws.filter((w) => w.kind === 'cloud')
check(cloudWires.length === 2 && cloudWires.every((w) => w.editable), 'one editable cloud wire per enabled provider model')
check(
  bs.find((b) => b.id === 'provider:openrouter').rows.length === 2,
  'a provider row per enabled model',
)
check(ws.filter((w) => w.kind === 'entry').length === setup.routing.length, 'the API Input reaches every load balancer')
check(ws.filter((w) => w.kind === 'link').length === 3, 'with three machines every pair draws its link')
check(ws.every((w) => w.type !== 'link' || !w.editable), 'links are never editable')

{
  const s = structuredClone(setup)
  for (const r of s.routing) r.policy = 'failover'
  const lb = D.blocks(D.fromServer(s), s)
  const split = lb.find((b) => b.id === 'split:llama-3.3-70b')
  check(split.ports.local === '1 · local' && split.ports.cloud === '2 · fallback', 'LB policy ranks local first, fallback second')
  check(split.policyChip === null, 'auto resolving to the LB policy shows no chip')
  const cloudOnly = lb.find((b) => b.id === 'split:anthropic/claude-sonnet-4.5')
  const autoOther = bs.find((b) => b.id === 'split:llama-3.3-70b')
  check(autoOther.policyChip === 'policy: weighted_capacity (auto)', 'auto resolving elsewhere names what it resolved to')
  check(cloudOnly.ports.cloud === '1 · cloud', 'a name with no local ranks its cloud port first')
}

{
  // A Save that landed: the next poll matches the draft, so nothing conflicts.
  const edited = D.removeProviderModel(base, 'openrouter', 'openai/gpt-4o-mini')
  const r = D.rebase(base, edited, edited)
  check(r.conflicts.length === 0 && D.diff(edited, r.doc, setup).length === 0, 'a saved edit folds into the server state with no conflict')
}

{
  // Moving a cloud wire to a new name brings a draft load balancer with it.
  const doc = D.addProviderModel(base, 'openrouter', 'meta-llama/llama-3.3-70b-instruct', 'brand-new')
  const split = D.blocks(doc, setup).find((b) => b.id === 'split:brand-new')
  check(split?.draft === true, 'a name a cloud wire creates gets a draft load balancer')
}

{
  // Live fallback: the local target stops admitting while a cloud row exists.
  const s = structuredClone(setup)
  for (const t of s.routing.find((r) => r.served_name === 'llama-3.3-70b').targets) if (t.kind === 'local') t.admitting = false
  const doc = D.addProviderModel(D.fromServer(s), 'openrouter', 'meta-llama/llama-3.3-70b-instruct', 'llama-3.3-70b')
  const w = D.wires(doc, s)
  check(w.filter((x) => x.kind === 'local' && x.served === 'llama-3.3-70b').every((x) => x.tone === 'fault' && x.label === 'not admitting'), 'a non-admitting local wire is red and says so')
  check(w.find((x) => x.kind === 'cloud' && x.served === 'llama-3.3-70b')?.label === 'fallback active', 'its cloud wire says the fallback is active')
  const none = D.validate(D.fromServer(s), s, {})
  check(
    none.some((i) => i.live && i.message === 'Llama-3.3-70B-Instruct on spark-01 is not admitting and llama-3.3-70b has no cloud fallback: requests are failing with 503.'),
    'with no cloud row the 503 is an error in Checks',
  )
}

// ── Draft models and launches ────────────────────────────────────────────────

{
  let doc = D.addModel(base, 'model:a', { model_id: 'Qwen/Qwen3-Next-80B-A3B-Instruct' })
  check(D.validate(doc, setup, {}).some((i) => i.message === 'Wire "deploy to" into at least one machine.'), 'an unwired draft model is an error')
  doc = D.connect(doc, { block: 'model:a', socket: 'deploy' }, { block: 'machine:spark-02', socket: 'deploy' })
  doc = D.connect(doc, { block: 'machine:spark-01', socket: 'deploy' }, { block: 'model:a', socket: 'deploy' })
  const m = doc.models[0]
  check(JSON.stringify(m.node_ids) === '["spark-01","spark-02"]', 'deploy to wires into two machines, either direction')
  const req = D.planRequest(m)
  check(req.parallelism?.tensor_parallel === 2, 'two machines is one instance across them: TP pinned to 2')
  check(D.planRequest(m, ['spark-01']).parallelism === undefined, 'one machine sends node_ids only')
  let [launch] = D.launches(doc, {})
  check(launch.speculative_tokens === 0, 'MTP off launches with speculative_tokens 0')
  doc = D.setModel(doc, 'model:a', { mtp: true, speculative_tokens: 3 })
  ;[launch] = D.launches(doc, {})
  check(launch.speculative_tokens === 3 && launch.node_ids.length === 2, 'MTP ticked launches with its draft tokens')
  check(D.diff(base, doc, setup).length === 0, 'draft models never become wiring ops')
  check(D.blocks(doc, setup).find((b) => b.id === 'model:a').mtp === null, 'no capability known: the block shows no MTP option')
  const cut = D.disconnect(doc, 'model:a|deploy|machine:spark-02|deploy')
  check(JSON.stringify(cut.models[0].node_ids) === '["spark-01"]', 'cutting a deploy wire drops that machine')
}

// ── rebase ───────────────────────────────────────────────────────────────────

{
  const s2 = structuredClone(setup)
  s2.providers[0].enabled = false
  const next = D.fromServer(s2)
  const draft = D.addLimit(base, 'limit:z', { rpm: 1 })
  const r = D.rebase(base, draft, next)
  check(r.doc.providers.openrouter.enabled === false, 'rebase follows an untouched provider')
  check(r.conflicts.length === 0, 'no conflict when only the server changed it')
  const edited = D.removeProviderModel(base, 'openrouter', 'openai/gpt-4o-mini')
  const r2 = D.rebase(base, edited, next)
  check(r2.conflicts.includes('provider:openrouter'), 'both sides changed: a conflict is reported')
  check('openai/gpt-4o-mini' in r2.doc.providers.openrouter.models === false, 'and the draft is kept')
  const taken = D.takeServer(r2.doc, next, 'provider:openrouter')
  check(taken.providers.openrouter.enabled === false && 'openai/gpt-4o-mini' in taken.providers.openrouter.models, 'Take server restores that entity')
  const v = D.validate(r2.doc, s2, {}, r2.conflicts)
  check(v.some((i) => i.message === 'openrouter changed on the server while you were editing.'), 'a conflict is a warning in Checks')

  let launched = D.addModel(base, 'model:q', { model_id: 'meta-llama/Llama-3.3-70B-Instruct', node_ids: ['ws-3090'] })
  const r3 = D.rebase(base, launched, base, setup.cluster.deployments)
  check(r3.doc.models.length === 0, 'a draft model drops out once it runs on the same machines')
  launched = D.setModel(launched, 'model:q', { node_ids: ['spark-01'] })
  check(D.rebase(base, launched, base, setup.cluster.deployments).doc.models.length === 1, 'but not on different machines')
}

// ── dealPositions ────────────────────────────────────────────────────────────

{
  const a = D.dealPositions(bs, {})
  const b = D.dealPositions(bs, {})
  check(JSON.stringify(a) === JSON.stringify(b), 'dealPositions is deterministic')
  check(bs.every((x) => a[x.id] && Number.isFinite(a[x.id].x) && Number.isFinite(a[x.id].y)), 'every block gets a position')
  check(a['merge:llama-3.3-70b'].y === a['split:llama-3.3-70b'].y, 'each merge sits level with its split')
  check(a.input.x < a['split:llama-3.3-70b'].x && a['split:llama-3.3-70b'].x < a['machine:spark-01'].x && a['machine:spark-01'].x < a['merge:llama-3.3-70b'].x && a['merge:llama-3.3-70b'].x < a.output.x, 'columns read left to right')
  const order = setup.routing.map((r) => a[`split:${r.served_name}`].y)
  check(order.every((y, k) => k === 0 || y > order[k - 1]), 'splits stack in routing order')
  const stored = D.dealPositions(bs, { input: { x: -500, y: 7 }, gone: { x: 1, y: 1 } })
  check(stored.input.x === -500 && stored.input.y === 7, 'a stored position wins')
  check(!('gone' in stored), 'a stored position for a block that no longer exists is ignored')
}

{
  // Overlaps: two dealt blocks that would collide are pushed apart by the gap.
  const sizes = { a: { w: 200, h: 300 }, b: { w: 200, h: 100 } }
  const out = D.resolveOverlaps({ a: { x: 0, y: 0 }, b: { x: 50, y: 120 } }, sizes)
  check(out.a.y === 0 && out.b.y - (out.a.y + sizes.a.h) >= D.GAP, `a dealt block that overlaps moves below by at least the gap (${out.b.y})`)
  const kept = D.resolveOverlaps({ a: { x: 0, y: 0 }, b: { x: 50, y: 120 } }, sizes, new Set(['b']))
  check(kept.b.x === 50 && kept.b.y === 120, 'a stored block is never moved')
  check(kept.a.y >= 120 + sizes.b.h + D.GAP, 'the dealt one gets out of its way instead')
  check(JSON.stringify(D.resolveOverlaps({ a: { x: 0, y: 0 }, b: { x: 400, y: 0 } }, sizes)) === '{"a":{"x":0,"y":0},"b":{"x":400,"y":0}}', 'blocks that do not touch stay put')

  // Measured heights: a machine far taller than its estimate pushes the
  // provider under it down, and the same sizes deal the same layout.
  const tall = { 'machine:spark-01': { w: 304, h: 900 } }
  const p1 = D.dealPositions(bs, {}, tall)
  check(JSON.stringify(p1) === JSON.stringify(D.dealPositions(bs, {}, tall)), 'dealPositions with sizes is deterministic')
  check(p1['machine:spark-02'].y >= p1['machine:spark-01'].y + 900 + D.GAP, 'the next block in a column stacks under the measured height')
  const rects = bs.map((b) => ({ id: b.id, ...p1[b.id], ...D.sizeOf(b, tall) }))
  const overlapping = rects.flatMap((r, k) =>
    rects.slice(k + 1).filter((o) => r.x < o.x + o.w && o.x < r.x + r.w && r.y < o.y + o.h && o.y < r.y + r.h).map((o) => `${r.id}~${o.id}`),
  )
  check(overlapping.length === 0, `no two dealt blocks overlap (${overlapping.join(', ')})`)
  // A block dragged onto another keeps its place; the dealt one moves.
  const dragged = D.dealPositions(bs, { 'machine:ws-3090': p1['machine:spark-01'] }, tall)
  check(JSON.stringify(dragged['machine:ws-3090']) === JSON.stringify(p1['machine:spark-01']), 'a dragged block stays where it was dropped')
  const under = dragged['machine:spark-01']
  const top = dragged['machine:ws-3090']
  check(under.y >= top.y + D.sizeOf(bs.find((b) => b.id === 'machine:ws-3090'), tall).h + D.GAP || under.y + 900 <= top.y, 'the block it landed on is moved out from under it')
}

{
  const s = structuredClone(setup)
  for (const r of s.routing) delete r.limits
  check(!D.limitsSupported(s), 'a gateway without per-name limits is detected')
  const blocks = D.blocks(D.addLimit(D.fromServer(s), 'limit:h'), s)
  check(!blocks.some((b) => b.kind === 'limit'), 'and the rate limit block is hidden')
  check(!blocks.find((b) => b.kind === 'split').sockets.some((x) => x.socket === 'limit'), 'with its socket')
}

// ── Round 2: policy, removal, flags, pins ────────────────────────────────────

{
  // Policy: a change pins it; a limit under auto saves as the triangle's policy.
  const pinned = D.setRoutePolicy(base, 'llama-3.3-70b', 'cache_affinity')
  check(
    JSON.stringify(D.diff(base, pinned, setup)) ===
      '[{"op":"route.set","served_name":"llama-3.3-70b","policy":"cache_affinity","limits":{}}]',
    'a policy change is a route.set with that policy and the current limits',
  )
  let limited = D.addLimit(base, 'limit:p', { rpm: 3 })
  limited = D.connect(limited, { block: 'limit:p', socket: 'out' }, { block: 'split:llama-3.3-70b', socket: 'limit' })
  check(D.diff(base, limited, setup)[0].policy === 'failover', 'a limit under auto is saved as failover')
  const s = structuredClone(setup)
  Object.assign(s.routing.find((r) => r.served_name === 'llama-3.3-70b'), { auto_selected: false, policy: 'round_robin', limits: { rpm: 3 } })
  const ob = D.fromServer(s)
  check(
    JSON.stringify(D.diff(ob, D.setRoutePolicy(ob, 'llama-3.3-70b', 'auto'), s)) ===
      '[{"op":"route.set","served_name":"llama-3.3-70b","policy":"failover","limits":{"rpm":3}}]',
    'back to auto while a limit is plugged: still a route.set, as failover',
  )
  const split = D.blocks(D.setRoutePolicy(base, 'llama-3.3-70b', 'round_robin'), setup).find((b) => b.id === 'split:llama-3.3-70b')
  check(split.policy === 'round_robin' && split.targets.length === 2 && split.removable, 'the split block carries its policy, targets and removability')
}

{
  // Removing a served name: its stops, its cloud rows, its route clear.
  const s = structuredClone(setup)
  const doc0 = D.addProviderModel(D.fromServer(s), 'openrouter', 'meta-llama/llama-3.3-70b-instruct', 'llama-3.3-70b')
  const gone = D.removeServedName(doc0, s, 'llama-3.3-70b')
  check(JSON.stringify(D.stops(gone)) === '["d-3090","d-spark"]', `its deployments are the stop list (${JSON.stringify(D.stops(gone))})`)
  const ops = D.diff(doc0, gone, s)
  check(
    JSON.stringify(ops.map((o) => o.op)) === '["provider.patch","route.clear"]' &&
      !ops[0].patch.enabled_models.includes('meta-llama/llama-3.3-70b-instruct'),
    'it switches its cloud rows off, then clears its route',
  )
  check(D.launches(gone, {}, s).length === 0, 'removal launches nothing')
  const sentence = D.removalSentence(doc0, s, 'llama-3.3-70b')
  check(
    sentence.startsWith('Remove llama-3.3-70b?\n\nStops: ') &&
      sentence.includes('(×2)\nSwitches off: openrouter:meta-llama/llama-3.3-70b-instruct (×1)\nClears: its routing override and limits\n\nNothing happens until you Save.'),
    'the confirm names what it stops and switches off',
  )
  const split = D.blocks(gone, s).find((b) => b.id === 'split:llama-3.3-70b')
  check(split?.removing === true, 'the removed name is still drawn, marked, until Save')
  check(D.blocks(gone, s).filter((b) => b.kind === 'machine').flatMap((b) => b.rows).every((r) => r.served !== 'llama-3.3-70b' || r.pending === 'stop'), 'its rows say they stop on Save')
  const after = D.rebase(doc0, gone, doc0, s.cluster.deployments.filter((d) => d.served_name !== 'llama-3.3-70b'))
  check(after.doc.removals.deployments.length === 0, 'rebase drops a stop once the deployment is gone')
}

{
  // Flags: drop one and the relaunch carries the rest, stopping the old one.
  const s = structuredClone(setup)
  const d = s.cluster.deployments.find((x) => x.deployment_id === 'd-spark')
  d.extra_args = ['--max-num-seqs', '8', '--enforce-eager', '--kv-cache-dtype=fp8']
  const doc0 = D.fromServer(s)
  check(JSON.stringify(D.flagGroups(d.extra_args)) === '[["--max-num-seqs","8"],["--enforce-eager"],["--kv-cache-dtype=fp8"]]', 'a flag groups with the values after it')
  const dropped = D.dropFlag(doc0, 'd-spark', d.extra_args, '--max-num-seqs')
  check(JSON.stringify(D.stops(dropped)) === '["d-spark"]', 'a relaunch stops the deployment first')
  const [req] = D.launches(dropped, {}, s)
  check(req?.extra_args === '--enforce-eager --kv-cache-dtype=fp8', `the relaunch keeps the other flags (${req?.extra_args})`)
  check(
    req.model_id === d.model_id && req.context === d.context_length && req.concurrency === d.max_concurrent_seqs && JSON.stringify(req.node_ids) === JSON.stringify(d.node_ids),
    'and the deployment’s own shape',
  )
  check(JSON.stringify(req.parallelism) === '{"pipeline_parallel":2}', 'parallelism from its plan, degrees above 1 only')
  check(D.diff(doc0, dropped, s).length === 0, 'a relaunch is never a wiring op')
  const row = D.blocks(dropped, s).find((b) => b.id === 'machine:spark-01').rows[0]
  check(row.pending === 'relaunch' && row.flags.find((f) => f.flag === '--max-num-seqs').removed, 'the row says relaunch pending, the flag struck')
  const both = D.dropFlag(dropped, 'd-spark', row.extra_args, '--enforce-eager')
  check(D.launches(both, {}, s)[0].extra_args === '--kv-cache-dtype=fp8', 'a second drop works from the pending args')
  check(D.rebase(doc0, dropped, doc0).doc.relaunches['d-spark'] !== undefined, 'rebase without deployments keeps the relaunch')
  check(D.rebase(doc0, dropped, doc0, []).doc.relaunches['d-spark'] === undefined, 'rebase drops it once the deployment is gone')
  const v = D.validate({ ...dropped }, { ...s, cluster: { ...s.cluster, deployments: [] } }, {})
  check(v.some((i) => i.message.endsWith('is gone; the flag change has nothing to relaunch.')), 'a relaunch with nothing to relaunch warns')
}

{
  // Pins: per-key maps, a cleared pin sends "" and false.
  const s = structuredClone(setup)
  const m = s.providers[0].models.find((x) => x.upstream_id === 'openai/gpt-4o-mini')
  m.backend_pin = 'azure'
  m.backend_fallback = true
  const doc0 = D.fromServer(s)
  check(JSON.stringify(doc0.providers.openrouter.pins) === '{"openai/gpt-4o-mini":{"tag":"azure","fallback":true}}', 'fromServer reads the pins off the models')
  const pinned = D.setProviderPin(doc0, 'openrouter', 'anthropic/claude-sonnet-4.5', { tag: 'google-vertex', fallback: true })
  check(
    JSON.stringify(D.diff(doc0, pinned, s)[0].patch) ===
      '{"backend_pins":{"anthropic/claude-sonnet-4.5":"google-vertex"},"backend_fallbacks":{"anthropic/claude-sonnet-4.5":true}}',
    'a new pin sends its tag and its fallback',
  )
  const cleared = D.setProviderPin(doc0, 'openrouter', 'openai/gpt-4o-mini', null)
  check(
    JSON.stringify(D.diff(doc0, cleared, s)[0].patch) === '{"backend_pins":{"openai/gpt-4o-mini":""},"backend_fallbacks":{"openai/gpt-4o-mini":false}}',
    'a cleared pin sends "" and false',
  )
  const row = D.blocks(pinned, s).find((b) => b.id === 'provider:openrouter')
  check(row.pinned === 2 && row.rows.find((r) => r.upstream_id === 'anthropic/claude-sonnet-4.5').sub.includes('google-vertex'), 'a pinned row names its host')
}

{
  // The dialog's targets: what is wired, not only what is routed yet.
  let doc = D.addProviderModel(base, 'openrouter', 'meta-llama/llama-3.3-70b-instruct', 'llama-3.3-70b')
  doc = D.addProviderModel(doc, 'openrouter', 'openai/gpt-4o-mini', 'llama-3.3-70b')
  const ts = D.balancerTargets(doc, setup, 'llama-3.3-70b')
  const by = Object.fromEntries(ts.map((t) => [t.id, t]))
  check(by['d-spark']?.live && by['d-spark'].pending === null, 'a routed local carries its live figures')
  check(by['openrouter:meta-llama/llama-3.3-70b-instruct']?.pending === 'add' && by['openrouter:meta-llama/llama-3.3-70b-instruct'].live === null, 'a cloud row wired in the draft is listed as added on Save')
  check(ts.length === 4, `both locals and both wired cloud rows are listed (${ts.length})`)
  const moved = D.balancerTargets(doc, setup, 'openai/gpt-4o-mini')
  check(moved.length === 1 && moved[0].pending === 'remove', 'a target rewired elsewhere shows as removed on Save where it was')
}

{
  // Shares: two linked sliders, a split that sums to 100, a route.set.
  check(JSON.stringify(D.rebalance({ a: 50, b: 50 }, 'a', 80)) === '{"a":80,"b":20}', 'moving one of two sliders moves the other')
  const three = D.rebalance({ a: 50, b: 30, c: 20 }, 'a', 0)
  check(three.a === 0 && three.b + three.c === 100 && three.b > three.c, `the rest is spread in proportion (${JSON.stringify(three)})`)
  check(JSON.stringify(D.rebalance({ a: 100, b: 0, c: 0 }, 'a', 40)) === '{"a":40,"b":30,"c":30}', 'evenly when the others had nothing')
  const ts = D.balancerTargets(base, setup, 'llama-3.3-70b')
  const measured = D.targetSplit(base, 'llama-3.3-70b', ts)
  check(!measured.manual && Object.values(measured.split).reduce((a, b) => a + b, 0) === 100, 'the measured split shows in whole percents summing to 100')
  const doc = D.setRouteShares(base, 'llama-3.3-70b', D.rebalance(measured.split, 'd-spark', 70))
  const [op] = D.diff(base, doc, setup)
  check(
    JSON.stringify(op) === '{"op":"route.set","served_name":"llama-3.3-70b","policy":"weighted_capacity","limits":{},"shares":{"d-3090":30,"d-spark":70}}',
    `a split by hand is a route.set under weighted_capacity (${JSON.stringify(op)})`,
  )
  check(D.targetSplit(doc, 'llama-3.3-70b', ts).manual, 'and the sliders then show it as set by hand')
  const away = D.setRoutePolicy(doc, 'llama-3.3-70b', 'round_robin')
  check(JSON.stringify(away.routes['llama-3.3-70b'].shares) === '{}', 'another policy drops the split')
  check(D.diff(base, away, setup)[0].shares === undefined, 'and an unchanged (empty) split is not sent')
}

check(D.modelTitle('Qwen/Qwen3-Next-80B-A3B-Instruct-FP8') === 'Qwen3-Next-80B-A3B-Instruct · FP8', 'a quant suffix reads as a chip-style tail')

done()
