import { useEffect, useReducer } from 'react'
import { avatarUrl, ownerAccent, ownerInitials, subscribeAvatars } from './owner'

/** A publisher's mark: their avatar, or an accent tile with their monogram.
 *
 *  One component for both sizes, because the decision it makes — is there an
 *  avatar to draw, and what colour is the tile behind it — is the same on a
 *  44px card and on an 18px list row, and it was previously written out inside
 *  `ModelCard`.
 *
 *  **`variant` picks a CSS class pair rather than emitting inline sizes.** The
 *  card keeps `.mcard-avatar`/`.mcard-initials` exactly as they were, so the
 *  models grid is unchanged by construction and not merely by inspection.
 *
 *  **This does not subscribe to anything, deliberately.** `avatarUrl` is
 *  synchronous and answers `null` until its batch lands, so something has to
 *  re-render — but that something is the LIST, via `useAvatars` below. A
 *  subscription in here would mean several hundred cards each holding their
 *  own state, re-rendering the whole grid several hundred times as one batch
 *  settles, which is the exact problem `owner.ts` documents.
 */
export function OwnerMark({
  owner,
  variant,
  accent,
  reserve,
}: {
  /** The publisher. Empty draws nothing at all — `gpt2` has no publisher and
   *  `ngram` is a mechanism, and a tile for either would invent one. */
  owner: string
  variant: 'card' | 'inline'
  /** Draw an empty tile-sized gap when there is no publisher, so a list keeps
   *  one column. Off by default: on a card a missing mark means "still
   *  resolving" and a blank square would read as a broken image, whereas in a
   *  row it means "this one genuinely has no publisher" and the ragged left
   *  edge it leaves reads as a bug. */
  reserve?: boolean
  /** Overrides the hashed accent. The card passes `dominantColor(url)`, which
   *  reads the avatar's own pixels back; the inline variant has no use for it
   *  at 18px and passes nothing. */
  accent?: string | null
}) {
  if (!owner) {
    return reserve ? <span className="omark omark-empty" aria-hidden="true" /> : null
  }
  const url = avatarUrl(owner)
  const tile = variant === 'card' ? 'mcard-avatar' : 'omark'
  const mono = variant === 'card' ? 'mcard-initials' : 'omark-initials'
  return (
    <span className={tile} style={{ background: accent ?? ownerAccent(owner) }}>
      {url ? (
        <img src={url} alt="" loading="lazy" decoding="async" />
      ) : (
        <span className={mono}>{ownerInitials(owner)}</span>
      )}
    </span>
  )
}

/** Re-render this list when a batch of avatars lands.
 *
 *  **Call this once per LIST, never inside a tile.** `avatarUrl` queues a name
 *  and answers `null` until the batch resolves, so a list that does not
 *  subscribe sits on monograms for ever and reads as a broken feature. The
 *  other half of that rule is `OwnerMark` above, which must not subscribe.
 */
export function useAvatars(): void {
  const [, bump] = useReducer((n: number) => n + 1, 0)
  useEffect(() => subscribeAvatars(bump), [])
}
