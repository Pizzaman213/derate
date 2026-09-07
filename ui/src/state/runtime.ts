// Which runtime a model is being served on, and the one thing every screen
// needs to know about that choice: whether it goes through the launcher.
//
// `vllm` and `sglang` mean plan -> fit gate -> sparkrun, onto machines this
// cluster owns and measures. `ollama` means telling a provider -- a box on the
// LAN that derate does not orchestrate -- to fetch a GGUF onto itself. Two
// different verbs behind one control, and almost every difference downstream
// follows from that one fact rather than from the runtime's name.
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

export type Runtime = 'vllm' | 'sglang' | 'tts' | 'ollama'

export interface RuntimeOption {
  value: Runtime
  /** What the picker shows. `(CPU)` on ollama is the case it exists for -- a
   *  GPU-less box that can still serve a small model -- and not a claim
   *  derate checks: a provider is a URL, and nothing here probes what is
   *  behind it. */
  label: string
}

/** One list, so the model inspector and the dashboard cannot drift. */
export const RUNTIME_OPTIONS: RuntimeOption[] = [
  { value: 'vllm', label: 'vllm' },
  { value: 'sglang', label: 'sglang' },
  { value: 'tts', label: 'tts (speech)' },
  { value: 'ollama', label: 'ollama (CPU)' },
]

/** The runtime that can actually load a model of this kind.
 *
 *  Not a preference and not a default that can be ignored: `vllm` and
 *  `sglang` have no `/v1/audio/speech` at all, so a text-to-speech checkpoint
 *  under either is refused by `/api/deployments` with the resolver's own
 *  sentence. Picking it for the reader beats letting them find that out from
 *  a 400 -- and the picker still lists every runtime, because the refusal is
 *  worth being able to read.
 */
export function runtimeFor(modality: Modality | undefined): Runtime {
  return modality === 'speech' ? 'tts' : 'vllm'
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
 *  them. The degree fields are hidden rather than shown and then rejected. */
export function shardsAcrossNodes(runtime: Runtime): boolean {
  return runtime !== 'tts' && servesOnCluster(runtime)
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
