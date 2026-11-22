import type { Profile } from '../../api/types'
import type { Resource } from '../../state/backend'
import { OwnerMark, useAvatars } from './OwnerMark'
import { ownerOf } from './owner'
import { lastResultLine, profilePill, profileSummary } from './profiles'

/** The Profiles view: every saved launch setup, across every model, newest
 *  first as `GET /api/profiles` answers. Load opens the model and applies the
 *  setup through the session's `applyRequest`; it never launches. */
export function ProfilesList({
  profiles,
  words,
  onLoad,
}: {
  profiles: Resource<Profile[]>
  words: string
  onLoad(profile: Profile): void
}) {
  useAvatars()
  const all = profiles.data ?? []
  const needle = words.trim().toLowerCase()
  const shown = needle
    ? all.filter(
        (p) => p.name.toLowerCase().includes(needle) || p.model_id.toLowerCase().includes(needle),
      )
    : all

  if (profiles.error) return <p className="label mdl-fatal">{profiles.error.message}</p>
  if (!shown.length)
    return (
      <p className="unit">
        {profiles.loading && !profiles.data
          ? 'Loading…'
          : needle
            ? `No saved profile matches ${words.trim()}.`
            : 'No saved profiles yet. Open a model, tune it on the Advanced tab, and save it as a profile.'}
      </p>
    )

  return (
    <ul className="mdl-proflist">
      {shown.map((p) => (
        <li key={p.profile_id} className="mdl-proflist-row">
          <OwnerMark owner={ownerOf(p.model_id)} variant="inline" reserve />
          <span className="mdl-proflist-main">
            <span className="mdl-proflist-name">
              <span className="mdl-ellipsis">{p.name}</span>
              <span className="mdl-pill">{p.is_default ? '★ default' : profilePill(p, [])}</span>
            </span>
            <span className="unit mono mdl-ellipsis" title={p.model_id}>
              {p.model_id}
            </span>
            <span className="unit">
              {[new Date(p.updated_at * 1000).toLocaleString(), lastResultLine(p)].filter(Boolean).join(' · ')}
            </span>
            <span className="mdl-pills">
              {profileSummary(p.request).map((chip) => (
                <span key={chip} className="mdl-pill">
                  {chip}
                </span>
              ))}
            </span>
          </span>
          <button type="button" onClick={() => onLoad(p)}>
            Load
          </button>
        </li>
      ))}
    </ul>
  )
}
