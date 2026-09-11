// Which runtime a model is being served on, and the one thing every screen
// needs to know about that choice: whether it goes through the launcher.
//
// `vllm`, `sglang`, `tts` and `llamacpp` mean plan -> fit gate -> sparkrun,
// onto machines this cluster owns and measures. `ollama` means telling a
// provider -- a box on the LAN that derate does not orchestrate -- to fetch a
// GGUF onto itself. Two different verbs behind one control, and almost every
// difference downstream follows from that one fact rather than from the
// runtime's name.
//
// `llamacpp` is the newest and sits on the cluster side of that line, which is
// the whole point of it: a machine with no GPU used to be reachable only as a
// provider -- something you ran Ollama on yourself and pointed derate at --
// and is now a serving node like any other, planned, fit-gated, launched and
// restarted by this control plane.
//
// Deliberately its own module rather than a union repeated in each picker.
// The list lived in five places as a bare `'vllm' | 'sglang'`, and neither
// `tabs/dashboard` nor `tabs/models` can own it without the other importing
// across. Sibling to `placement.ts` for the reason that file gives: this is an
// argument to a request, not a selection.
//
// Not in the URL. `?on=`/`?tp=`/`?pp=` are there because a verdict is only
// worth sending to somebody if the question it answers travels with it, and a
// runtime does not change a verdict -- under `ollama` there is no verdict at
// all. Putting it in the scheme would also mean `?rt=ollama` parsing back on a
// screen where it means nothing. If that changes, it goes in alongside the
// provider choice as one announced change to `routes.ts` and its checker.

import type { Modality } from '../api/types'

export type Runtime = 'vllm' | 'sglang' | 'tts' | 'llamacpp' | 'ollama'

export interface RuntimeOption {
  value: Runtime
  /** What the picker shows.
   *
   *  `(CPU)` appears twice and means two different strengths of thing. On
   *  `ollama` it is the case the option exists for and NOT a claim derate
   *  checks -- a provider is a URL, and nothing here probes what is behind it.
   *  On `llamacpp` it is checked: the placement gate refuses that runtime on a
   *  machine that has GPU memory, and refuses a GPU runtime on one that has
   *  none (`deploy/flags.py::placement_refusal`). */
  label: string
}

/** One list, so the model inspector and the dashboard cannot drift. */
export const RUNTIME_OPTIONS: RuntimeOption[] = [
  { value: 'vllm', label: 'vllm' },
  { value: 'sglang', label: 'sglang' },
  { value: 'tts', label: 'tts (speech)' },
  { value: 'llamacpp', label: 'llamacpp (CPU)' },
  { value: 'ollama', label: 'ollama (CPU)' },
]

/** Which pool of memory this runtime places a model in.
 *
 *  Mirrors `RuntimeSpec.memory_pool` on the server, and exists for the same
 *  reason: "has no GPU memory" and "cannot serve" were one question for the
 *  life of this project, and are two now. Every screen that asked the old one
 *  has to ask this instead, or the board offers ticks the launch refuses.
 */
export function memoryPool(runtime: Runtime): 'gpu' | 'host' {
  return runtime === 'llamacpp' ? 'host' : 'gpu'
}

/** Whether a machine of this shape can carry a rank of this runtime.
 *
 *  The client half of `deploy/flags.py::placement_refusal`, and deliberately
 *  only the half that can be answered from a node row: this says whether the
 *  HARDWARE is the right kind, and the server additionally refuses a CPU node
 *  whose memory nothing has measured.
 *
 *  Both directions are refusals. A GPU runtime needs GPU memory, which is the
 *  obvious one. `llamacpp` refusing a machine that HAS a GPU is the one worth
 *  stating properly, because the tempting reason is the wrong one: it is not
 *  that llama.cpp would waste the card. It would run there, on the CPU, and
 *  nothing would break. It is that derate cannot budget it there --
 *  `registry.allocatable_bytes` reports host memory only for a CPU device
 *  class, so the fit gate would size a host-RAM launch against a GPU figure.
 *  An accounting limit in this build, not a fact about llama.cpp.
 */
export function canCarryRank(
  runtime: Runtime,
  node: { addressable_memory: number },
): boolean {
  return memoryPool(runtime) === 'host'
    ? node.addressable_memory <= 0
    : node.addressable_memory > 0
}

/** The runtime that can actually load a model of this kind, on this cluster.
 *
 *  Two rules, and they are different in kind -- which is why the reason is
 *  returned alongside rather than left for the reader to infer.
 *
 *  Modality is a REQUIREMENT. `vllm` and `sglang` have no
 *  `/v1/audio/speech` at all, so a text-to-speech checkpoint under either is
 *  refused by `/api/deployments` with the resolver's own sentence. Picking it
 *  for the reader beats letting them find that out from a 400.
 *
 *  Hardware is a RECOMMENDATION. A cluster with no GPU cannot run vllm or
 *  sglang anywhere -- `placement_refusal` says so on every machine in it --
 *  so arriving on a model page with `vllm` selected means arriving at a red
 *  verdict with no hint that one control away is a runtime that runs here.
 *  That was the argument for picking `tts` on a speech model and it is the
 *  same argument.
 *
 *  Both are defaults rather than locks. The picker still lists every runtime,
 *  because the refusal is worth being able to read, and the caller sets this
 *  on arrival rather than deriving it at render so that choosing `vllm` to
 *  read its refusal is not undone by the next poll.
 *
 *  An empty node list means the cluster has not loaded yet, NOT that it has no
 *  GPU -- so it recommends nothing and leaves the default alone. A screen that
 *  flips the picker to CPU for one frame while the roster arrives is worse
 *  than one that never moves.
 */
export function runtimeFor(
  modality: Modality | undefined,
  nodes?: readonly { profile: { addressable_memory: number } }[],
): { runtime: Runtime; because: string | null } {
  if (modality === 'speech') return { runtime: 'tts', because: null }
  if (nodes && nodes.length > 0 && !nodes.some((n) => n.profile.addressable_memory > 0)) {
    return {
      runtime: 'llamacpp',
      because:
        'No machine here has GPU memory, so vllm and sglang have nowhere to ' +
        'run. llamacpp serves GGUF builds on the CPU.',
    }
  }
  return { runtime: 'vllm', because: null }
}

/** Whether this runtime launches onto a machine in the roster.
 *
 *  The single predicate every branch reads, so "ollama does not go through
 *  the launcher" is stated once instead of being re-derived as
 *  `runtime === 'ollama'` in each of the places that care -- the plan call,
 *  the node board, the degrees, the verdict, and Serve itself.
 */
export function servesOnCluster(runtime: Runtime): boolean {
  return runtime !== 'ollama'
}

/** Whether this runtime shards a model across ranks.
 *
 *  `tts` is one process holding one checkpoint, so TP and PP are not degrees
 *  it has -- `deploy/flags.py::sharding_refusal` refuses a plan that carries
 *  them. The degree fields are hidden rather than shown and then rejected.
 *
 *  `llamacpp` is the same answer for a weaker reason, and the difference is
 *  worth keeping: llama.cpp DOES have a mode that spans machines (RPC), and
 *  nothing in this build has run it, so `shards=False` on the server is a
 *  statement about what has been verified rather than about what exists. */
export function shardsAcrossNodes(runtime: Runtime): boolean {
  return runtime !== 'tts' && runtime !== 'llamacpp' && servesOnCluster(runtime)
}

/** Narrow an arbitrary string to a Runtime, falling back to the default.
 *
 *  Only for values arriving from outside the type system (a stored
 *  preference, a URL, a deployment record). An unrecognised runtime becomes
 *  `vllm` rather than `null`, because every caller needs *a* runtime and a
 *  silent null would render an empty picker.
 */
export function asRuntime(value: string | null | undefined): Runtime {
  return RUNTIME_OPTIONS.some((o) => o.value === value)
    ? (value as Runtime)
    : 'vllm'
}
