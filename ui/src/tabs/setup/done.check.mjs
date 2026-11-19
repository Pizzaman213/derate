// The setup wizard's done screen: what it says is running, and how a launch
// failure is split for display. Hermetic -- done.ts is pure.
//
//   node src/tabs/setup/done.check.mjs
//
// The bug this exists for: the done screen trusted the model step's own flag
// and titled a launch that had already failed "starting", with a green dot.
// It reads the deployment records now, and the pick's record wins over
// anything else that happens to be served.

import { load, report } from '../../check/harness.mjs'

const D = await load(import.meta.url, './done.ts')
const { check, done } = report()

const pick = { model: { row: { model_id: 'Qwen/Qwen2.5-0.5B-Instruct-GPTQ-Int4', label: 'Qwen2.5 0.5B' } } }
const dep = (state, extra = {}) => ({
  deployment_id: `d-${state}`,
  served_name: 'qwen2.5-0.5b',
  model_id: 'Qwen/Qwen2.5-0.5B-Instruct-GPTQ-Int4',
  state,
  last_error: null,
  node_ids: ['spark-01'],
  ...extra,
})
const served = (id) => ({ id, context_length: null, target_count: 1, target_kinds: ['remote'] })
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b)

// serving
check(eq(D.doneReading(pick, [dep('ready')], []), { kind: 'serving', model: 'qwen2.5-0.5b', local: true }), 'ready pick: serving under its served name')
check(eq(D.doneReading(pick, [dep('degraded')], []), { kind: 'serving', model: 'qwen2.5-0.5b', local: true }), 'degraded pick: still serving')
check(eq(D.doneReading({ model: null }, [], [served('qwen2.5:0.5b')]), { kind: 'serving', model: 'qwen2.5:0.5b', local: false }), 'no pick, a provider model served: serving it, not on this machine')
check(
  eq(
    D.doneReading({ model: null }, [dep('ready', { model_id: 'other/model' })], [served('qwen2.5:0.5b'), served('qwen2.5-0.5b')]),
    { kind: 'serving', model: 'qwen2.5-0.5b', local: true },
  ),
  'no pick: a name a local deployment answers to wins over a provider name listed first',
)

// loading
check(eq(D.doneReading(pick, [dep('launching')], []), { kind: 'loading', model: 'qwen2.5-0.5b' }), 'launching pick: loading')
check(eq(D.doneReading(pick, [dep('planned')], []), { kind: 'loading', model: 'qwen2.5-0.5b' }), 'planned pick: loading')
check(
  eq(D.doneReading(pick, [dep('failed', { last_error: 'x' }), dep('launching')], []), { kind: 'loading', model: 'qwen2.5-0.5b' }),
  'a relaunch in flight beats the dead record it replaced',
)

// failed
const dump = 'Docker is not reachable from this node.\nsparkrun exited 1\n--- sparkrun output ---\nError: dial unix /var/run/docker.sock'
const failed = D.doneReading(pick, [dep('failed', { last_error: dump })], [])
check(failed.kind === 'failed', 'failed pick: failed')
check(failed.model === 'Qwen2.5 0.5B', 'failed names the pick by its label')
check(failed.why === 'Docker is not reachable from this node.', `why is the first line only (got "${failed.why}")`)
check(
  D.doneReading(pick, [dep('failed', { last_error: dump })], [served('qwen2.5:0.5b')]).kind === 'failed',
  'pick failed but a provider model is served: failed -- the pick wins',
)
check(
  D.doneReading(pick, [dep('failed')], []).why === 'the launcher reported a failure',
  'no last_error: a stated fallback, never an empty line',
)

// nothing
check(eq(D.doneReading({ model: null }, [], []), { kind: 'nothing' }), 'no pick, nothing served: nothing')
check(eq(D.doneReading(pick, [dep('stopped')], []), { kind: 'nothing' }), 'a stopped pick with nothing served: nothing')
check(
  eq(D.doneReading(pick, [{ ...dep('ready'), model_id: 'other/model' }], []), { kind: 'nothing' }),
  'another model\'s deployment is not the pick\'s',
)

// splitLaunchFailure
const split = D.splitLaunchFailure(dump)
check(split.head === 'Docker is not reachable from this node.\nsparkrun exited 1', 'head is everything before the dump marker, verbatim')
check(split.output === 'Error: dial unix /var/run/docker.sock', 'output is everything after it, verbatim')
const plain = D.splitLaunchFailure('Will not fit: 3.2 GiB over budget.')
check(plain.head === 'Will not fit: 3.2 GiB over budget.' && plain.output === null, 'no marker: the whole text is the head, no output')

done()
