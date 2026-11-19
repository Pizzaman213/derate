// sockets.ts: the socket table, canConnect's ten rules in order, the fixed
// sentences, and what lights up during a drag.
//
//   cd ui && node src/tabs/cluster/sockets.check.mjs
import { load, report } from '../../check/harness.mjs'

const S = await load(import.meta.url, './sockets.ts')
const { check, done } = report()

const rows = [{ id: 'dep:d1', title: 'Llama-3.3-70B-Instruct', served: 'llama' }]
const pmRows = [{ id: 'pm:or:x/y', title: 'x/y', served: 'llama' }]
const blocks = {
  input: S.socketsOf('input', 'input', 'API Input'),
  output: S.socketsOf('output', 'output', 'API Output'),
  splitA: S.socketsOf('split', 'split:llama', 'llama load balancer'),
  splitB: S.socketsOf('split', 'split:qwen', 'qwen load balancer'),
  merge: S.socketsOf('merge', 'merge:llama', 'llama merge'),
  machine: S.socketsOf('machine', 'machine:spark-01', 'spark-01', rows),
  machine2: S.socketsOf('machine', 'machine:spark-02', 'spark-02', []),
  provider: S.socketsOf('provider', 'provider:or', 'OpenRouter', pmRows),
  limit: S.socketsOf('limit', 'limit:a', 'Rate limit'),
  model: S.socketsOf('model', 'model:a', 'Draft model'),
}
const get = (b, socket) => blocks[b].find((s) => s.socket === socket)

// ── The socket table ─────────────────────────────────────────────────────────

const expected = [
  ['input', 'out', 'requests', 'out', false],
  ['splitA', 'in', 'requests', 'in', false],
  ['splitA', 'limit', 'limit', 'in', true],
  ['splitA', 'local', 'requests', 'out', false],
  ['splitA', 'cloud', 'requests', 'out', true],
  ['machine', 'in:dep:d1', 'requests', 'in', false],
  ['machine', 'out:dep:d1', 'responses', 'out', false],
  ['provider', 'in:pm:or:x/y', 'requests', 'in', true],
  ['provider', 'out:pm:or:x/y', 'responses', 'out', false],
  ['machine', 'deploy', 'deploy', 'in', true],
  ['merge', 'local', 'responses', 'in', false],
  ['merge', 'cloud', 'responses', 'in', false],
  ['merge', 'out', 'responses', 'out', false],
  ['output', 'in', 'responses', 'in', false],
  ['model', 'deploy', 'deploy', 'out', true],
  ['machine', 'link-top', 'link', 'in', false],
  ['machine', 'link-bottom', 'link', 'in', false],
  ['limit', 'out', 'limit', 'out', true],
]
for (const [b, socket, type, dir, editable] of expected) {
  const s = get(b, socket)
  check(
    s && s.type === type && s.dir === dir && s.editable === editable,
    `${b}.${socket}: ${type} ${dir}, ${editable ? 'editable' : 'fixed'}`,
  )
}
check(get('splitA', 'limit').edge === 'bottom', "the split's limit socket sits on its lower edge")
check(get('splitA', 'local').label === 'local' && blocks.splitA.indexOf(get('splitA', 'local')) < blocks.splitA.indexOf(get('splitA', 'cloud')), 'local is above cloud')
check(get('machine', 'deploy').label === 'deploy here' && get('model', 'deploy').label === 'deploy to', 'deploy sockets are labelled')
check(Object.values(S.SOCKET_TYPES).every((t) => t.colour === `var(--${t.token})` && t.token.startsWith('graph-')), 'every type is drawn in its own graph-* token')
check(S.LB_POLICY === 'failover', 'the triangle stands for failover')

// Sides: inputs left, outputs right, with exactly two exceptions -- the
// machine link (top/bottom, display only) and the split's limit (bottom,
// drawn on the triangle's lower slanted edge).
const SIDE_EXCEPTIONS = { 'machine-link': ['top', 'bottom'], 'split-limit': ['bottom'] }
const wrongSide = Object.values(blocks)
  .flat()
  .filter((s) => {
    const allowed = SIDE_EXCEPTIONS[s.role] ?? [s.dir === 'in' ? 'left' : 'right']
    return !allowed.includes(s.edge)
  })
  .map((s) => `${s.block}.${s.socket} (${s.role}, ${s.dir}) on ${s.edge}`)
check(wrongSide.length === 0, `every input sits left and every output right, bar the two exceptions${wrongSide.length ? `: ${wrongSide.join('; ')}` : ''}`)
check(get('machine', 'link-top').edge === 'top' && get('machine', 'link-bottom').edge === 'bottom', 'machine links sit top and bottom')

// ── canConnect, one sentence per rule ────────────────────────────────────────

const N = S.SENTENCES
const cases = [
  [get('splitA', 'cloud'), get('splitA', 'in'), N.self, 'rule 1: a block to itself'],
  [get('splitA', 'cloud'), get('splitB', 'cloud'), N.bothOut, 'rule 2: two outputs'],
  [get('provider', 'in:pm:or:x/y'), get('splitB', 'in'), N.bothIn, 'rule 2: two inputs'],
  [get('splitA', 'cloud'), get('splitB', 'limit'), 'Requests sockets only join requests sockets; that one takes limit.', 'rule 3: different types'],
  [get('machine', 'out:dep:d1'), get('output', 'in'), N.responses, 'rule 4: responses'],
  [get('machine2', 'link-bottom'), get('machine', 'link-top'), S.SENTENCES.bothIn, 'link sockets are both inputs'],
  [get('provider', 'in:pm:or:x/y'), get('input', 'out'), N.apiInput, 'rule 6: the API Input'],
  [get('provider', 'in:pm:or:x/y'), get('splitA', 'local'), N.localToCloud, 'rule 7: local port to a provider row'],
  [
    get('splitB', 'local'),
    get('machine', 'in:dep:d1'),
    "Llama-3.3-70B-Instruct serves 'llama' and always sits on that load balancer's local port. Relaunch it under another name to move it.",
    'rule 7: local port to a machine row',
  ],
  [get('splitA', 'cloud'), get('machine', 'in:dep:d1'), N.cloudToLocal, 'rule 8: cloud port to a machine row'],
]
for (const [from, to, sentence, what] of cases) check(S.canConnect(from, to) === sentence, what)

{
  // Rule 5 needs an output link socket, which no block has; drive it directly.
  const out = { ...get('machine2', 'link-bottom'), dir: 'out' }
  check(S.canConnect(out, get('machine', 'link-top')) === N.link, 'rule 5: links')
}
const cloud = get('splitA', 'cloud')
const row = get('provider', 'in:pm:or:x/y')
check(S.canConnect(cloud, row) === null, 'rule 10: cloud port to a provider row connects')
check(S.canConnect(row, cloud) === null, '... from either end')
check(S.canConnect(cloud, row, () => true) === N.already, 'rule 9: already wired')
check(S.canConnect(get('limit', 'out'), get('splitB', 'limit')) === null, 'a limit plugs into a split')
check(S.canConnect(get('model', 'deploy'), get('machine', 'deploy')) === null, 'a draft model deploys to a machine')

// ── Fixed sockets ────────────────────────────────────────────────────────────

const F = S.FIXED_SENTENCES
const fixed = Object.values(blocks).flat().filter((s) => !s.editable)
check(fixed.every((s) => S.fixedSentence(s) !== null), 'every fixed socket has a sentence for when it is pressed')
check(Object.values(blocks).flat().filter((s) => s.editable).every((s) => S.fixedSentence(s) === null), 'no editable socket says it is fixed')
check(S.fixedSentence(get('merge', 'out')) === F.responses, 'responses sockets say so')
check(S.fixedSentence(get('machine', 'link-top')) === F.link, 'link sockets say so')
check(S.fixedSentence(get('splitA', 'local')) === F.local && S.fixedSentence(get('machine', 'in:dep:d1')) === F.local, 'the local port and a machine row say so')
check(S.fixedSentence(get('input', 'out')) === F.entry && S.fixedSentence(get('splitA', 'in')) === F.entry, 'the entry sockets say so')

// ── Dragging ─────────────────────────────────────────────────────────────────

const all = Object.values(blocks).flat()
const lit = (from) => all.filter((s) => S.dragClass(from, s) === 'valid').map((s) => `${s.block}.${s.socket}`)
check(JSON.stringify(lit(cloud)) === JSON.stringify(['provider:or.in:pm:or:x/y']), 'a cloud drag lights only provider rows')
check(
  JSON.stringify(lit(get('limit', 'out'))) === JSON.stringify(['split:llama.limit', 'split:qwen.limit']),
  'a limit drag lights only split limit sockets',
)
check(S.dragClass(cloud, cloud) === 'source', 'the socket a drag starts on is its source')
check(S.SENTENCES.dragHint('requests', 'llama load balancer') === 'Dragging requests from llama load balancer: drop on a glowing requests socket.', 'the drag hint')

{
  const onMachine = S.dropOnBlock(cloud, blocks.machine, 'spark-01')
  check('refusal' in onMachine && onMachine.refusal === N.cloudToLocal, 'a cloud wire dropped on a machine body gets the local-vs-cloud refusal')
  const onProvider = S.dropOnBlock(cloud, blocks.provider, 'OpenRouter')
  check('socket' in onProvider && onProvider.socket.socket === 'in:pm:or:x/y', 'dropped on a provider body it takes the first row')
  const onOutput = S.dropOnBlock(get('limit', 'out'), blocks.output, 'API Output')
  check('refusal' in onOutput && onOutput.refusal === 'Nothing on API Output takes limit.', 'nothing takes it: the block says so')
}

done()
