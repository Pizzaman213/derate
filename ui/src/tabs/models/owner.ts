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
// `GET https://huggingface.co/api/organizations/{name}/overview` carries an
// `avatarUrl`; a personal namespace answers on `/api/users/{name}/overview`
// instead. Both are unauthenticated and rate limited, which is what every
// precaution below is for.

type Entry =
  | { kind: 'url'; url: string }
  | { kind: 'gone' }
  | { kind: 'retry'; until: number; failures: number }

const CACHE_MAX = 256
const RETRY_BASE_MS = 60_000
const RETRY_MAX_MS = 30 * 60_000
const TIMEOUT_MS = 10_000
/** A burst of cards must not become a burst of requests: the hub rate limits an
 *  unauthenticated client hard, and a 429 poisons the whole grid. */
const MAX_CONCURRENT = 6

const cache = new Map<string, Entry>()
const inflight = new Map<string, Promise<string | null>>()
const listeners = new Set<() => void>()
let active = 0
const queue: (() => void)[] = []

function remember(owner: string, entry: Entry): void {
  // Cheapest possible LRU: re-inserting moves a key to the end, so the first
  // key is the oldest. A grid holds far fewer than 256 distinct owners.
  cache.delete(owner)
  cache.set(owner, entry)
  if (cache.size > CACHE_MAX) {
    const oldest = cache.keys().next().value
    if (oldest !== undefined) cache.delete(oldest)
  }
}

function cached(owner: string): Entry | null {
  const entry = cache.get(owner)
  if (!entry) return null
  // An expired backoff reports "nothing cached" so the caller retries, but the
  // entry stays so the next failure escalates rather than restarting at 60s.
  if (entry.kind === 'retry' && Date.now() >= entry.until) return null
  return entry
}

async function acquire(): Promise<void> {
  if (active < MAX_CONCURRENT) {
    active++
    return
  }
  await new Promise<void>((resolve) => {
    queue.push(() => {
      active++
      resolve()
    })
  })
}

function release(): void {
  active--
  queue.shift()?.()
}

async function lookup(owner: string): Promise<string | null> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), TIMEOUT_MS)
  try {
    for (const path of ['organizations', 'users']) {
      const res = await fetch(
        `https://huggingface.co/api/${path}/${encodeURIComponent(owner)}/overview`,
        { signal: controller.signal },
      )
      if (res.status === 404) continue
      if (!res.ok) throw new Error(String(res.status))
      const data = (await res.json()) as { avatarUrl?: string }
      if (!data.avatarUrl) continue
      return data.avatarUrl.startsWith('http')
        ? data.avatarUrl
        : `https://huggingface.co${data.avatarUrl}`
    }
    return null
  } finally {
    window.clearTimeout(timer)
  }
}

function notify(): void {
  for (const fn of listeners) fn()
}

/** Resolve an owner's avatar, at most once per owner, at most six at a time.
 *
 *  Returns synchronously from cache when it can. A miss is remembered two ways:
 *  a 404 is permanent (that namespace has no avatar and never will within a
 *  session), while a timeout or a 5xx backs off and doubles, so a rate limit
 *  recovers on its own instead of hammering. */
export function avatarUrl(owner: string): string | null {
  const key = owner.trim()
  if (!key) return null
  const entry = cached(key)
  if (entry) return entry.kind === 'url' ? entry.url : null
  if (inflight.has(key)) return null

  const run = (async () => {
    await acquire()
    try {
      const url = await lookup(key)
      remember(key, url ? { kind: 'url', url } : { kind: 'gone' })
      return url
    } catch {
      const prev = cache.get(key)
      const failures = prev?.kind === 'retry' ? prev.failures + 1 : 1
      remember(key, {
        kind: 'retry',
        failures,
        until: Date.now() + Math.min(RETRY_BASE_MS * 2 ** (failures - 1), RETRY_MAX_MS),
      })
      return null
    } finally {
      release()
      inflight.delete(key)
      notify()
    }
  })()
  inflight.set(key, run)
  return null
}

/** Re-render the cards when a lookup lands. One subscription per grid rather
 *  than per card: several hundred cards each holding their own state would
 *  re-render the whole grid several hundred times as the batch resolves. */
export function subscribeAvatars(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}
