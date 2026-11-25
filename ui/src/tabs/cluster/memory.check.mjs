// memory.ts: a machine's used memory, split by the processes holding it.
//
//   cd ui && node src/tabs/cluster/memory.check.mjs
import { load, report } from '../../check/harness.mjs'

const M = await load(import.meta.url, './memory.ts')
const { check, done } = report()

const GB = 2 ** 30
const proc = (pid, name, gb, dep = null, served = null) => ({
  pid, name, command: null, user: null, gpu_memory: gb * GB,
  deployment_id: dep, served_name: served, deployment_state: dep ? 'ready' : null, killable: dep == null, not_killable_reason: null,
})

{
  const parts = M.memoryParts(60 * GB, [
    proc(11, 'python3', 8, 'd-1', 'qwen'),
    proc(12, 'VLLM::EngineCore', 20, 'd-1', 'qwen'),
    proc(40, 'vllm', 25),
    proc(41, 'llama-server', 0),
  ])
  check(parts.length === 3, `a deployment's processes fold into one part, a zero-byte process is dropped (got ${parts.length})`)
  check(parts[0].label === 'qwen' && parts[0].bytes === 28 * GB && parts[0].managed === true, 'a derate deployment is named by its served name')
  check(parts[1].managed === false && parts[1].bytes === 25 * GB && parts[1].label.includes('not managed here'), 'a process derate did not launch is shown and says so')
  check(parts[2].key === 'rest' && parts[2].bytes === 7 * GB && parts[2].managed === null, 'what no process holds is its own remainder')
}
{
  const parts = M.memoryParts(10 * GB, [proc(40, 'vllm', 12)])
  check(parts.length === 1 && parts[0].bytes === 10 * GB && parts[0].held === 12 * GB, 'the bar is clamped to the sample, the list keeps the real size')
  const lag = M.memoryParts(20 * GB, [proc(11, 'vllm', 20, 'd-1', 'qwen'), proc(40, 'python', 3)])
  check(lag.length === 2 && lag[1].bytes === 0 && lag[1].held === 3 * GB, 'a process past a lagging sample is still listed, never dropped')
}
{
  const orphan = { ...proc(50, 'vllm', 9, 'd-9', 'old'), deployment_state: 'stopped', killable: true }
  const [p] = M.memoryParts(9 * GB, [orphan])
  check(p.managed === false && p.label === 'vllm (pid 50, left by stopped old)', 'a process under a stopped record is named an orphan, not a serving model')
}
{
  const deps = Array.from({ length: 6 }, (_, i) => proc(i + 1, 'vllm', 1, `d-${i}`, `m${i}`))
  const parts = M.memoryParts(66 * GB, [...deps, proc(99, 'stray', 60)])
  check(parts[0].label === 'stray (pid 99, not managed here)', 'the fold never hides the largest holder behind "N more"')
}
{
  const parts = M.memoryParts(10.2 * GB, [proc(40, 'vllm', 10)])
  check(parts.length === 1, 'a remainder under the node board floor is not drawn')
  check(M.memoryParts(10 * GB, [proc(1, 'x', Infinity), proc(2, 'y', NaN)]).every((p) => Number.isFinite(p.bytes)), 'a non-finite process size is dropped')
}
{
  const many = Array.from({ length: 9 }, (_, i) => proc(100 + i, `p${i}`, 1))
  const parts = M.memoryParts(40 * GB, many)
  const more = parts.find((p) => p.key === 'more')
  check(parts.filter((p) => p.key.startsWith('pid:')).length === M.MAX_LISTED && more?.bytes === 4 * GB && more.label === '4 more processes', 'past MAX_LISTED, the tail folds into one part')
}
{
  const parts = M.memoryParts(10 * GB, null)
  check(parts.length === 1 && parts[0].label === 'used (processes not read)' && parts[0].bytes === 10 * GB, 'an unread process list is not "nothing resident"')
}
{
  check(M.memoryParts(0, []).length === 0, 'nothing used, nothing listed: no parts')
}

done()
