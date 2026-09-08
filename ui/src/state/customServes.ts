import { useEffect, useState } from 'react'
import type { Runtime } from './runtime'

/** One launch's custom command, remembered so the same one -- a
 *  quantization flag, a memory tuning knob, anything the standard recipe
 *  does not cover -- can be replayed onto a model without retyping it.
 *
 *  Client-side only, unlike everything `custom_command` touches from here
 *  on: this list has no sizing implication and picking an entry only seeds
 *  the Serve panel's field, so the plan and the fit gate still run exactly
 *  as they would for anything typed by hand, and the M-22 allowlist
 *  (deploy/recipes.py) still checks every token again on replay. */
export interface CustomServe {
  modelId: string
  runtime: Runtime
  target: 'throughput' | 'latency'
  command: string
  lastUsed: number
}

const KEY = 'derate.models.customServes'
//: Same-tab reactivity. `storage` fires in every OTHER window, never the one
//: that wrote the key, and the box has to update the moment a launch in THIS
//: tab succeeds.
const CHANGED = 'derate:custom-serves-changed'
//: A screenful, not a database: past this, the box is scrolling rather than
//: skimming, and the oldest tweak is the one least likely to still matter.
const MAX = 12

function isCustomServe(v: unknown): v is CustomServe {
  if (!v || typeof v !== 'object') return false
  const r = v as Record<string, unknown>
  return (
    typeof r.modelId === 'string' &&
    typeof r.runtime === 'string' &&
    typeof r.target === 'string' &&
    typeof r.command === 'string' &&
    typeof r.lastUsed === 'number'
  )
}

function load(): CustomServe[] {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(isCustomServe).sort((a, b) => b.lastUsed - a.lastUsed)
  } catch {
    // A corrupt value here is not this screen's problem to diagnose --
    // starting empty is always safe, since nothing here is a source of
    // truth for anything the launcher does.
    return []
  }
}

function persist(list: CustomServe[]): void {
  window.localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX)))
  window.dispatchEvent(new Event(CHANGED))
}

function sameServe(
  a: CustomServe,
  b: Pick<CustomServe, 'modelId' | 'runtime' | 'command'>,
): boolean {
  return a.modelId === b.modelId && a.runtime === b.runtime && a.command === b.command
}

/** Save a launch's custom command, or bump it to the front if it repeats.
 *
 *  Called once, from `ServePanel`, and only after `backend.launch` has
 *  already resolved -- a launch the backend rejected taught nothing worth
 *  replaying. */
export function recordCustomServe(entry: {
  modelId: string
  runtime: Runtime
  target: 'throughput' | 'latency'
  command: string
}): void {
  const rest = load().filter((s) => !sameServe(s, entry))
  persist([{ ...entry, lastUsed: Date.now() }, ...rest])
}

export function removeCustomServe(
  entry: Pick<CustomServe, 'modelId' | 'runtime' | 'command'>,
): void {
  persist(load().filter((s) => !sameServe(s, entry)))
}

/** The list, kept live across a launch on this same screen (the `CHANGED`
 *  event) and across a change made in another tab (`storage`) -- there is no
 *  poll, because nothing but this tab's own launches and its own deletes
 *  ever changes it. */
export function useCustomServes(): CustomServe[] {
  const [list, setList] = useState<CustomServe[]>(() => load())
  useEffect(() => {
    const refresh = () => setList(load())
    window.addEventListener(CHANGED, refresh)
    window.addEventListener('storage', refresh)
    return () => {
      window.removeEventListener(CHANGED, refresh)
      window.removeEventListener('storage', refresh)
    }
  }, [])
  return list
}
