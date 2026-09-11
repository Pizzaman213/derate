/** Owner identity on a card: the accent colour, and the avatar behind it.
 *
 *  Modelled on how Unsloth Studio's hub cards read -- a coloured tile per
 *  publisher, the real HuggingFace avatar when there is one -- reimplemented
 *  here. Their `studio/**` tree is AGPL-3.0-only and derate has no licence of
 *  its own, so this is written from the behaviour, not from the source.
 *
 *  The palette is decorative, which is a deliberate exception to
 *  `agents/H-ui.md`'s "nothing is coloured decoratively" rule and is confined
 *  to this one screen. The three signal colours keep their meaning: an accent
 *  identifies a publisher, a lamp or a status dot reports state, and they never
 *  share a hue by accident because the palette below excludes the signal hues. */

/** Ten hues, hashed by owner, so the same publisher is the same colour on every
 *  card and across reloads. Deliberately not `--live`/`--warn`/`--fault`: an
 *  accent that landed on signal green would read as a verdict. */
const PALETTE = [
  'hsl(214 66% 52%)',
  'hsl(172 54% 44%)',
  'hsl(30 70% 52%)',
  'hsl(348 62% 56%)',
  'hsl(198 64% 50%)',
  'hsl(140 48% 46%)',
  'hsl(222 50% 56%)',
  'hsl(16 64% 54%)',
  'hsl(260 46% 58%)',
  'hsl(44 72% 50%)',
]

/** The publisher half of a model id, or `''` for a bare repository.
 *
 *  `''` is load-bearing and is NOT the same as "a publisher we could not
 *  identify": `gpt2` has no publisher at all, and `ngram` is a mechanism rather
 *  than a repository. Both must draw no mark, because a tile there would invent
 *  an owner for something that has none.
 */
export function ownerOf(modelId: string): string {
  const slash = modelId.indexOf('/')
  return slash > 0 ? modelId.slice(0, slash) : ''
}

/** The repository half — the whole id when there is no publisher. */
export function repoOf(modelId: string): string {
  const slash = modelId.indexOf('/')
  return slash > 0 ? modelId.slice(slash + 1) : modelId
}

export function hashString(value: string): number {
  let h = 0
  for (let i = 0; i < value.length; i++) h = (h * 31 + value.charCodeAt(i)) | 0
  return Math.abs(h)
}

export function ownerAccent(owner: string): string {
  const name = owner.trim() || '?'
  return PALETTE[hashString(name) % PALETTE.length]!
}

/** Two letters, for the tile shown until (or instead of) an avatar. */
export function ownerInitials(owner: string): string {
  const cleaned = owner.replace(/[^A-Za-z0-9]+/g, ' ').trim()
  if (!cleaned) return '?'
  const words = cleaned.split(/\s+/)
  if (words.length > 1) return (words[0]![0]! + words[1]![0]!).toUpperCase()
  return cleaned.slice(0, 2).toUpperCase()
}

/** Publishers whose own repositories are the canonical upload, marked with a
 *  check the way Studio marks its own. Not a quality claim -- it says the
 *  weights come from the people who trained them. */
const FIRST_PARTY = new Set([
  'unsloth',
  'openai',
  'qwen',
  'meta-llama',
  'google',
  'deepseek-ai',
  'mistralai',
  'microsoft',
  'nvidia',
  'allenai',
])

export function isFirstParty(owner: string): boolean {
  return FIRST_PARTY.has(owner.trim().toLowerCase())
}

// ── Avatar lookup ────────────────────────────────────────────────────────────
//
// The coordinator resolves these now, and this file asks it once for the whole
// grid. What was here before went straight to the hub, once per publisher, on
// every page load -- with an LRU, a six-wide semaphore and an exponential
// backoff, all of it trying to stay under an unauthenticated rate limit that a
// single Models grid is already over. It did not work: ~45 publishers tripped
// the limit, the backoff doubled from a minute towards half an hour, and every
// card sat on two letters for that whole window. It looked intermittent
// because the limit is a burst window that recovers on its own.
//
// None of that machinery is needed against our own origin. `control_plane/
// resolver/avatars.py` asks the hub once per publisher EVER, keeps the bytes
// on disk and serves them same-origin, so the client's whole job is to name
// the publishers on screen and read back which ones have a mark.

/** ready -> a same-origin path to draw, `null` -> this publisher has no mark. */
const known = new Map<string, string | null>()
const listeners = new Set<() => void>()

/** Named on screen, not yet asked about. */
const pending = new Set<string>()
/** Asked about and left out of the answer, which means "still resolving". */
const unresolved = new Set<string>()
/** How many times each publisher has been asked about and not settled. */
const attempts = new Map<string, number>()
let scheduled = false
let inflight = 0

/** How many publishers go in one request. Matches the server's own cap, which
 *  is a bound on fan-out at the hub rather than a pagination scheme. */
const BATCH = 64
/** One frame's worth of card renders, coalesced. A grid mounts its cards in a
 *  burst, so waiting a tick turns ~90 calls into one request. */
const COALESCE_MS = 16
/** A cold batch runs to the server's deadline and reports what it has; the
 *  rest are asked for again. Long enough that a slow hub does not become a
 *  poll, short enough that the marks appear while the grid is still on screen. */
const RETRY_MS = 1500
/** How many rounds in a row may settle nothing before this gives up. Counts
 *  UNPRODUCTIVE rounds only: a grid of 93 publishers is two full batches and
 *  neither is a stall, whereas a coordinator that is down answers nothing
 *  however many times it is asked. The monogram is a perfectly good card, so
 *  the right end state there is to stop, not to keep hammering. */
const MAX_STALLS = 4
let stalls = 0

/** How many times ONE publisher may come back unsettled before it draws a
 *  monogram and stops being asked for.
 *
 *  Separate from the stall counter, which is about the coordinator being down.
 *  This is about the one name that never settles -- a publisher the hub keeps
 *  rate limiting us on, say. Without it a batch where 63 of 64 resolve keeps
 *  its counter at zero forever and polls our own coordinator for the 64th
 *  every retry, for as long as the tab is open. */
const MAX_ATTEMPTS = 4

function notify(): void {
  for (const fn of listeners) fn()
}

function schedule(delay = COALESCE_MS): void {
  if (scheduled || pending.size === 0 || stalls >= MAX_STALLS) return
  scheduled = true
  window.setTimeout(() => {
    scheduled = false
    void flush()
  }, delay)
}

/** Ask about this publisher again, unless it has had its turns. */
function requeue(owner: string): void {
  const tries = (attempts.get(owner) ?? 0) + 1
  attempts.set(owner, tries)
  if (tries >= MAX_ATTEMPTS) known.set(owner, null)
  else pending.add(owner)
}

async function flush(): Promise<void> {
  if (inflight > 0 || pending.size === 0) return
  const batch = [...pending].slice(0, BATCH)
  batch.forEach((owner) => pending.delete(owner))
  batch.forEach((owner) => unresolved.add(owner))
  inflight++
  let settled = 0
  try {
    const query = batch.map(encodeURIComponent).join(',')
    const res = await fetch(`/api/publishers/avatars?owners=${query}`)
    if (!res.ok) throw new Error(String(res.status))
    const data = (await res.json()) as { avatars?: Record<string, string | null> }
    const answers = data.avatars ?? {}
    for (const owner of batch) {
      // Present -> settled, either way. Absent -> the server is still
      // resolving it, so it goes back in the queue rather than being recorded
      // as a publisher without a mark.
      if (owner in answers) {
        known.set(owner, answers[owner] ?? null)
        unresolved.delete(owner)
        attempts.delete(owner)
        settled++
      } else {
        unresolved.delete(owner)
        requeue(owner)
      }
    }
  } catch {
    // Our own coordinator, not the hub. Put them back; the stall counter is
    // what stops this if it is really down.
    for (const owner of batch) {
      unresolved.delete(owner)
      requeue(owner)
    }
  } finally {
    inflight--
    // A round that answered everything it asked is not a stall, so the next
    // chunk goes immediately; one that answered nothing waits, and enough of
    // those in a row stop the loop.
    stalls = settled > 0 ? 0 : stalls + 1
    notify()
    schedule(settled === batch.length ? COALESCE_MS : RETRY_MS)
  }
}

/** Where this publisher's mark is served, or null while there is nothing to
 *  draw -- which covers both "not resolved yet" and "has none".
 *
 *  Synchronous, and safe to call from render: a name it has not seen is queued
 *  and the grid is told to re-render when the answer lands. Same-origin, so
 *  there is no CORS to arrange and `dominant.ts` can read the pixels back. */
export function avatarUrl(owner: string): string | null {
  const key = owner.trim()
  if (!key) return null
  const entry = known.get(key)
  if (entry !== undefined) return entry
  if (!pending.has(key) && !unresolved.has(key)) {
    pending.add(key)
    // A name nobody has asked about means the grid changed, so a run of
    // stalls stops counting against it -- otherwise a publisher scrolled to
    // after the coordinator had a bad minute would silently never resolve.
    stalls = 0
  }
  schedule()
  return null
}

/** Re-render the cards when a batch lands. One subscription per grid rather
 *  than per card: several hundred cards each holding their own state would
 *  re-render the whole grid several hundred times as the batch resolves. */
export function subscribeAvatars(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

