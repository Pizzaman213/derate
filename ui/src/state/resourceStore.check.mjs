// The shared resource store: one read per name however many subscribe, no
// read for a name nobody is looking at, none while the browser tab is hidden.
// Every one of those is invisible to types -- a regression here shows up only
// as a coordinator flooded by its own UI, which is how it was found.
//
//   node src/state/resourceStore.check.mjs

import { load, report } from '../check/harness.mjs'

const S = await load(import.meta.url, './resourceStore.ts')
const { check, done } = report()

/** A clock nobody but the test moves. */
function manualTimers() {
  let now = 0
  let seq = 1
  const queue = new Map()
  const timers = {
    setTimeout(fn, ms) {
      const id = seq++
      queue.set(id, { at: now + ms, fn })
      return id
    },
    clearTimeout(id) {
      queue.delete(id)
    },
    now: () => now,
  }
  const settle = async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve()
  }
  /** Fire every timer due by `now + ms`, in order, letting each read settle. */
  const advance = async (ms) => {
    const end = now + ms
    for (;;) {
      await settle()
      let next = null
      for (const [id, t] of queue) if (t.at <= end && (!next || t.at < next.t.at)) next = { id, t }
      if (!next) break
      queue.delete(next.id)
      now = Math.max(now, next.t.at)
      next.t.fn()
    }
    now = end
    await settle()
  }
  return { timers, advance, settle }
}

/** A read that counts its calls. `manual` reads wait for `resolve()`/`reject()`. */
function fakeRead({ manual = false } = {}) {
  const f = { calls: 0, pending: [] }
  f.read = () => {
    f.calls += 1
    if (!manual) return Promise.resolve({ n: f.calls })
    return new Promise((resolve, reject) => f.pending.push({ resolve, reject }))
  }
  f.resolve = (v) => f.pending.shift().resolve(v)
  f.reject = (e) => f.pending.shift().reject(e)
  return f
}

const backend = {}

// --- one read for every subscriber, at the interval --------------------------
{
  const { timers, advance } = manualTimers()
  const store = new S.ResourceStore(backend, timers)
  const r = fakeRead()
  const offA = store.register('cluster', r.read, 5000)
  const offB = store.register('cluster', r.read, 5000)
  await advance(0)
  check(r.calls === 1, `two registrations of one name read once (read ${r.calls})`)
  await advance(5000)
  check(r.calls === 2, `after one interval exactly one more read (read ${r.calls})`)
  offA()
  offB()
  await advance(60000)
  check(r.calls === 2, `no registration left, no read over a minute (read ${r.calls})`)
  check(store.snapshot('cluster').data?.n === 2, 'unregistering keeps the last answer')
}

// --- the shortest live interval wins ----------------------------------------
{
  const { timers, advance } = manualTimers()
  const store = new S.ResourceStore(backend, timers)
  const r = fakeRead()
  store.register('x', r.read, 10000)
  const fast = store.register('x', r.read, 2000)
  await advance(0)
  await advance(4000)
  check(r.calls === 3, `a 2s and a 10s subscriber poll at 2s (read ${r.calls} in 4s)`)
  fast()
  await advance(9999)
  check(r.calls === 3, `the 2s subscriber gone, the next read waits for 10s (read ${r.calls})`)
  await advance(1)
  check(r.calls === 4, `...and comes at 10s (read ${r.calls})`)
}

// --- a failed refresh keeps the last good value ------------------------------
{
  const { timers, advance } = manualTimers()
  const store = new S.ResourceStore(backend, timers)
  const r = fakeRead({ manual: true })
  let notified = 0
  store.listen('s', () => { notified += 1 })
  store.register('s', r.read, 5000)
  await advance(0)
  r.resolve({ ok: 1 })
  await advance(0)
  const good = store.snapshot('s')
  check(good.data?.ok === 1 && good.error === null && good.loading === false, 'a success is data, no error, not loading')
  check(notified === 1, `a settled read notifies its listeners (${notified})`)
  check(store.snapshot('s') === good, 'snapshot() is the same object until something changes')
  await advance(5000)
  r.reject(new Error('boom'))
  await advance(0)
  const bad = store.snapshot('s')
  check(bad.data?.ok === 1, 'a failure after a success keeps the data')
  check(bad.error instanceof Error && bad.error.message === 'boom', 'a failure after a success sets the error')
  check(bad.loading === false, 'a failure is not loading')
  check(bad !== good, 'a change replaces the snapshot')
}

// --- a non-Error rejection is wrapped -----------------------------------------
{
  const { timers, advance } = manualTimers()
  const store = new S.ResourceStore(backend, timers)
  const r = fakeRead({ manual: true })
  store.register('w', r.read, 5000)
  await advance(0)
  r.reject('plain string')
  await advance(0)
  const snap = store.snapshot('w')
  check(snap.error instanceof Error && snap.error.message === 'plain string', 'a non-Error rejection is wrapped in Error')
  check(snap.data === null && snap.loading === false, 'a first failure is an error, not loading')
}

// --- a read in flight is never doubled ---------------------------------------
{
  const { timers, advance } = manualTimers()
  const store = new S.ResourceStore(backend, timers)
  const r = fakeRead({ manual: true })
  store.register('slow', r.read, 1000)
  await advance(0)
  await advance(60000)
  check(r.calls === 1, `ticks while a read is in flight start no second one (read ${r.calls})`)
  store.refreshActive()
  await advance(0)
  check(r.calls === 1, `a refresh during the read starts no second one either (read ${r.calls})`)
  r.resolve({ v: 1 })
  await advance(0)
  check(r.calls === 2, `...but is not lost: the read after it goes at once (read ${r.calls})`)
}

// --- refreshActive reads registered names only -------------------------------
{
  const { timers, advance } = manualTimers()
  const store = new S.ResourceStore(backend, timers)
  const a = fakeRead()
  const b = fakeRead()
  store.register('a', a.read, 30000)
  const offB = store.register('b', b.read, 30000)
  await advance(0)
  offB()
  await advance(1000)
  store.refreshActive()
  await advance(0)
  check(a.calls === 2, `refreshActive reads a registered name at once (read ${a.calls})`)
  check(b.calls === 1, `refreshActive does not read an unregistered one (read ${b.calls})`)
  store.register('b', b.read, 30000)
  await advance(0)
  check(b.calls === 2, `...which reads on its next registration (read ${b.calls})`)
}

// --- a fresh name does not refetch on a second registration -------------------
{
  const { timers, advance } = manualTimers()
  const store = new S.ResourceStore(backend, timers)
  const r = fakeRead()
  const off = store.register('f', r.read, 30000)
  await advance(0)
  off()
  await advance(1000)
  store.register('f', r.read, 30000)
  await advance(0)
  check(r.calls === 1, `re-registering a fresh name waits out its interval (read ${r.calls})`)
}

// --- a hidden browser tab polls nothing --------------------------------------
{
  const { timers, advance } = manualTimers()
  const store = new S.ResourceStore(backend, timers)
  const r = fakeRead()
  store.register('v', r.read, 5000)
  await advance(0)
  store.setVisible(false)
  await advance(60000)
  check(r.calls === 1, `setVisible(false): no read over a minute (read ${r.calls})`)
  store.setVisible(true)
  await advance(0)
  check(r.calls === 2, `setVisible(true): a stale name reads at once (read ${r.calls})`)
}

// --- a store whose subscribers all left goes idle -----------------------------
// The provider relies on this rather than a close(): StrictMode runs cleanups
// twice in development, and a replaced store must stop by itself.
{
  const { timers, advance } = manualTimers()
  const store = new S.ResourceStore(backend, timers)
  const r = fakeRead({ manual: true })
  const off = store.register('c', r.read, 1000)
  await advance(0)
  off()
  r.resolve({ late: true })
  await advance(60000)
  check(r.calls === 1, `a read settling after its last subscriber left schedules nothing (read ${r.calls})`)
}

// --- a name nobody fetched ---------------------------------------------------
{
  const store = new S.ResourceStore(backend, manualTimers().timers)
  check(store.snapshot('never') === S.EMPTY_SNAPSHOT, "snapshot('never') is EMPTY_SNAPSHOT")
  check(S.EMPTY_SNAPSHOT.loading === true && S.EMPTY_SNAPSHOT.data === null, 'EMPTY_SNAPSHOT is loading with no data')
  check(Object.isFrozen(S.EMPTY_SNAPSHOT), 'EMPTY_SNAPSHOT is frozen')
}

done()
