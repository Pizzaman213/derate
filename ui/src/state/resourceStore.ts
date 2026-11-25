// One fetch per endpoint, however many components ask.
//
// `useResource` polls per call site, and `useCluster` alone is called from
// more than twenty files -- so one browser sent bursts of sixteen identical
// `/api/cluster` reads a second and ~200 a minute, and the coordinator's access
// log was 99.8% of one session's polls. A store keyed by name collapses those
// into one timer and one read: every subscriber sees the same snapshot, the
// interval is the shortest any live registration asked for, and a name with no
// live registration does not poll at all (its last answer is kept for the next
// one). Page visibility pauses every timer store-wide.
//
// React-free on purpose: `check.mjs` bundles with `platform: 'neutral'`, and
// `resourceStore.check.mjs` drives this with a manual clock.

import type { Backend } from '../api/client'

export interface Snapshot {
  data: unknown
  error: Error | null
  loading: boolean
}

export interface Timers {
  setTimeout(fn: () => void, ms: number): number
  clearTimeout(id: number): void
  now(): number
}

/** Read at call time, so loading this module never touches `window`. */
export const browserTimers: Timers = {
  setTimeout: (fn, ms) => window.setTimeout(fn, ms),
  clearTimeout: (id) => window.clearTimeout(id),
  now: () => Date.now(),
}

/** What a name that has never been fetched reads as. One frozen object, so
 *  `useSyncExternalStore` sees a stable snapshot before the first answer. */
export const EMPTY_SNAPSHOT: Snapshot = Object.freeze({ data: null, error: null, loading: true })

type Read = (b: Backend) => Promise<unknown>

interface Entry {
  data: unknown
  error: Error | null
  /** When the last read settled. `-Infinity` means stale: fetch on schedule. */
  fetchedAt: number
  inflight: boolean
  /** A refresh arrived while a read was in flight. That read may have started
   *  before the change it is meant to see, so its answer does not count as
   *  fresh and the next read is scheduled at once. */
  refetch: boolean
  /** The most recent registration's read. */
  read: Read | null
  /** Registration id -> the interval it asked for. */
  regs: Map<number, number>
  listeners: Set<() => void>
  timer: number | null
  /** Replaced only when the entry changes, so its identity means "unchanged". */
  snap: Snapshot
}

export class ResourceStore {
  private readonly entries = new Map<string, Entry>()
  private nextId = 0
  private visible = true

  constructor(
    private readonly backend: Backend,
    private readonly timers: Timers = browserTimers,
  ) {}

  private entry(name: string): Entry {
    let e = this.entries.get(name)
    if (!e) {
      e = {
        data: null,
        error: null,
        fetchedAt: -Infinity,
        inflight: false,
        refetch: false,
        read: null,
        regs: new Map(),
        listeners: new Set(),
        timer: null,
        snap: EMPTY_SNAPSHOT,
      }
      this.entries.set(name, e)
    }
    return e
  }

  listen(name: string, onChange: () => void): () => void {
    const e = this.entry(name)
    e.listeners.add(onChange)
    return () => {
      e.listeners.delete(onChange)
    }
  }

  snapshot(name: string): Snapshot {
    return this.entries.get(name)?.snap ?? EMPTY_SNAPSHOT
  }

  /** A live subscriber. A stale or never-fetched name fetches at once; the
   *  returned function ends the registration, and the last one to end stops
   *  the timer. The data is kept for the next registration. */
  register(name: string, read: (b: Backend) => Promise<unknown>, intervalMs: number): () => void {
    const e = this.entry(name)
    const id = this.nextId++
    e.regs.set(id, intervalMs)
    e.read = read
    this.schedule(e)
    return () => {
      e.regs.delete(id)
      this.schedule(e)
    }
  }

  /** Something changed server-side: every name is stale. Registered names
   *  fetch now; the rest fetch on their next registration. */
  refreshActive(): void {
    for (const e of this.entries.values()) {
      e.fetchedAt = -Infinity
      if (e.inflight) e.refetch = true
    }
    for (const e of this.entries.values()) this.schedule(e)
  }

  /** A hidden browser tab polls nothing; showing it fetches what went stale. */
  setVisible(visible: boolean): void {
    this.visible = visible
    for (const e of this.entries.values()) this.schedule(e)
  }

  private clear(e: Entry): void {
    if (e.timer !== null) {
      this.timers.clearTimeout(e.timer)
      e.timer = null
    }
  }

  private schedule(e: Entry): void {
    this.clear(e)
    if (!this.visible || e.regs.size === 0) return
    const ms = Math.min(...e.regs.values())
    const delay = Math.max(0, e.fetchedAt + ms - this.timers.now())
    e.timer = this.timers.setTimeout(() => {
      e.timer = null
      void this.fetchNow(e)
    }, delay)
  }

  private async fetchNow(e: Entry): Promise<void> {
    // A tick during a read starts nothing: the settling read reschedules.
    if (e.inflight || !e.read) return
    e.inflight = true
    try {
      e.data = await e.read(this.backend)
      e.error = null
    } catch (err) {
      // Keep the last good value through a failed refresh.
      e.error = err instanceof Error ? err : new Error(String(err))
    } finally {
      e.inflight = false
      e.fetchedAt = e.refetch ? -Infinity : this.timers.now()
      e.refetch = false
      e.snap = { data: e.data, error: e.error, loading: e.data === null && e.error === null }
    }
    for (const fn of [...e.listeners]) fn()
    this.schedule(e)
  }
}
