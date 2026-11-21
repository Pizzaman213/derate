// Hermetic: `board.ts` against hand-built rosters, no coordinator.
//
// `rows.check.mjs` carries the board's live-cluster section and needs a
// logged-in gateway, so on a machine without one every assertion about which
// machine a host-pool runtime may tick is skipped. These are the ones that
// need nothing but the module: the board must offer exactly the machines
// `deploy/flags.py::placement_refusal` accepts, draw the pool the gate
// budgets, and say so in the server's own words.
//
//   cd ui && node src/tabs/models/board.check.mjs
import { load, report } from '../../check/harness.mjs'

const B = await load(import.meta.url, './board.ts')
const { check, done } = report()

const GIB = 1024 ** 3
const machine = (id, device_class, mem) => ({
  profile: {
    node_id: id, hostname: id, address: '10.0.0.1', device_class,
    gpu_name: mem ? 'GPU' : '', gpu_count: mem ? 1 : 0, total_memory: mem, addressable_memory: mem,
    memory_bandwidth_gbps: mem ? 273 : 0, compute_capability: mem ? '8.6' : '', driver_version: '',
  },
  healthy: true, state: 'healthy', role: 'worker', last_seen: 0,
  memory_used: 0, memory_total: mem, power_watts: null, temperature_c: null, utilization_pct: null,
})
const NOCACHE = { nodes: () => [], bytes: () => null, complete: () => null, all: () => [] }
const estate = [
  machine('spark', 'gb10', 120 * GIB),
  machine('rtx', 'discrete', 24 * GIB),
  machine('pi', 'cpu', 0),
  machine('odd', 'unknown', 0),
]
const memory = [
  { node_id: 'spark', allocatable: 90 * GIB, static_ceiling: 108 * GIB, host_allocatable: null, stale: false },
  { node_id: 'rtx', allocatable: 20 * GIB, static_ceiling: 21.6 * GIB, host_allocatable: 50 * GIB, stale: false },
  { node_id: 'pi', allocatable: 1 * GIB, static_ceiling: 0, host_allocatable: 1 * GIB, stale: false },
]
const board = (runtime) => B.buildBoard({
  nodes: estate, edges: [], memory, deployments: [], cache: NOCACHE, repoId: 'x/y',
  plannerChose: null, placement: null, chosen: null, runtime,
})
const row = (b, id) => b.rows.find((r) => r.nodeId === id)
const gpu = board('vllm')
const host = board('llamacpp')

// Which machines: the server's HOST_POOL_CLASSES for llamacpp, GPU memory for the rest.
check(['rtx', 'pi'].every((id) => row(host, id).selectable) && !row(host, 'spark').selectable
  && !row(host, 'odd').selectable, 'llamacpp ticks the discrete box and the CPU node, never the Spark or an unidentified machine')
check(row(gpu, 'rtx').selectable && row(gpu, 'spark').selectable && !row(gpu, 'pi').selectable,
  'vllm ticks the machines with GPU memory')

// Which pool is drawn: the one the gate budgets, never the card for a host-RAM launch.
check(row(host, 'rtx').hostPool === true && row(host, 'rtx').allocatable === 50 * GIB && row(host, 'rtx').ceiling === null,
  'under llamacpp the discrete box draws its host budget, with no nameplate behind it')
check(row(gpu, 'rtx').hostPool === false && row(gpu, 'rtx').allocatable === 20 * GIB && row(gpu, 'rtx').ceiling === 21.6 * GIB,
  'under vllm it draws the card, exactly as before')
check(row(host, 'spark').noHostPool === true && row(host, 'spark').allocatable === null,
  'a unified machine under llamacpp has no host pool to read, which is not "not read yet"')
check(row(host, 'rtx').noHostPool === false && row(gpu, 'spark').noHostPool === false,
  'a machine with a host pool, or any machine under a GPU runtime, is not flagged')

// In the server's words (`placement_refusal`), with "this machine" for the id.
const sparkWhy = row(host, 'spark').unselectableReason ?? ''
check(sparkWhy.startsWith('This machine has unified memory') && sparkWhy.includes('(vllm, sglang, tts)'),
  `the Spark's refusal is the unified-memory sentence and names every GPU runtime (${sparkWhy.slice(-40)})`)
check((row(host, 'odd').unselectableReason ?? '').startsWith('Nothing identified what kind of machine this is'),
  'an unidentified machine is refused for being unidentified, not for unified memory')
check(/Pick llamacpp/.test(row(gpu, 'pi').unselectableReason ?? ''),
  'a GPU runtime on a CPU node points at llamacpp, which would take it')
check(!/llamacpp/.test(row(gpu, 'odd').unselectableReason ?? 'missing') && row(gpu, 'odd').unselectableReason != null,
  'and does not point an unidentified machine at llamacpp, which would refuse it too')

done()
