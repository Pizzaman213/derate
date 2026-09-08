/** A provider's own mark on the bus, and the tile underneath it.
 *
 *  Deliberately NOT modelled on `tabs/models/owner.ts`. That module fetches
 *  HuggingFace avatars for several hundred cards at once from an
 *  unauthenticated, rate-limited third party, which is what its LRU-256, its
 *  six-way concurrency gate, its 10s abort and its doubling backoff are all
 *  for. None of that applies here: there are one to three providers on one
 *  bus, the URL is our own coordinator through `apiUrl`, there is no rate
 *  limit, and the browser's HTTP cache already deduplicates it. The
 *  coordinator does the caching, the backoff and the negative caching -- see
 *  `control_plane/providers/logos.py` -- so repeating any of it here would be
 *  two caches disagreeing about one picture.
 *
 *  What IS kept from owner.ts is the discipline: a miss is remembered, and it
 *  notifies so the drawing repaints once rather than every row holding its own
 *  state and re-requesting on each 5s poll.
 *
 *  The fallback is solved by paint order, not by a loading state. A failed
 *  `<image>` in SVG renders as NOTHING -- there is no broken-image glyph -- so
 *  the monogram tile is drawn ALWAYS and the mark painted on top of it. There
 *  is then no loading flicker, no blank frame, and a coordinator too old to
 *  have the route renders exactly what a vendor with no favicon renders.
 */

import { apiUrl } from '../../api/origin'

/** Provider ids whose mark 404s. Session-scoped: a provider whose logo lands
 *  after a coordinator restart should be picked up on the next reload, and a
 *  wrong entry here costs a monogram, not a broken picture. */
const missing = new Set<string>()
const listeners = new Set<() => void>()

export function providerLogoUrl(providerId: string): string {
  return apiUrl(`/api/providers/${encodeURIComponent(providerId)}/logo`)
}

export function providerLogoMissing(providerId: string): boolean {
  return missing.has(providerId)
}

/** Stop drawing an `<image>` that 404s over a perfectly good tile. */
export function markProviderLogoMissing(providerId: string): void {
  if (missing.has(providerId)) return
  missing.add(providerId)
  for (const fn of listeners) fn()
}

export function subscribeProviderLogos(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** One uppercase initial for the tile under the mark.
 *
 *  One letter, not two: the graph's type floor is 9 units and two letters
 *  inside an 11-unit tile would need about 7, under the floor the whole
 *  drawing keeps. It never has to disambiguate anything either -- the provider
 *  id is in the row text right beside it -- so this is a placeholder, not a
 *  label.
 *
 *  `displayName` first because an operator-minted id can be `p-3f2a`, whose
 *  initial says nothing, while its display name is "OpenRouter". */
export function providerMonogram(providerId: string, displayName?: string): string {
  const source = (displayName || '').trim() || (providerId || '').trim()
  const letter = source.replace(/[^A-Za-z0-9]/g, '').charAt(0)
  return letter ? letter.toUpperCase() : '?'
}
