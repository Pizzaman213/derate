import { useEffect, useState } from 'react'
import type { LaunchRequest, Profile } from '../../api/types'
import { Lamp } from '../../components/Lamp'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { useBenchmarks, useProfiles } from '../../state/resources'
import { memoryPool, type Runtime } from '../../state/runtime'
import { benchResult } from './BenchmarksTab'
import { lastResultLine, planBodyFrom, profilePill, profileSummary, type ProfilePill } from './profiles'
import { planWord, type Word } from './verdictView'
import type { ModelSession } from './useModelSession'

/** Saved setups and auto-saved runs for this model, newest first. Loading
 *  one only puts its settings back -- nothing launches from here; the
 *  verdict is taken again on the Serve tab before anything starts. */
export function ProfilesTab({
  s,
  modelId,
  onAdvancedTab,
  onServeTab,
}: {
  s: ModelSession
  modelId: string
  onAdvancedTab: () => void
  onServeTab: () => void
}) {
  const { backend, invalidate } = useBackend()
  const profiles = useProfiles(modelId)
  const benches = useBenchmarks(modelId)
  const [error, setError] = useState<string | null>(null)

  const act = (p: Promise<unknown>) =>
    p.then(
      () => {
        setError(null)
        invalidate()
      },
      (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
    )

  const save = () => {
    const n = (profiles.data?.length ?? 0) + 1
    const name = window.prompt('Name this setup', `Saved setup ${n}`)?.trim()
    if (!name) return
    void act(
      backend.createProfile({ model_id: modelId, name, request: s.currentRequest() }).then((p) => s.markSaved(p)),
    )
  }

  const list = profiles.data ?? []

  return (
    <div className="mdl-profiles">
      <div className="mdl-profiles-bar">
        <button type="button" onClick={save}>
          Save current settings
        </button>
      </div>
      {error ? <Verbatim text={error} size="unit" /> : null}
      {profiles.error ? <Verbatim text={profiles.error.message} size="unit" /> : null}
      {!list.length ? (
        <p className="unit">
          No saved setups or previous runs for this model yet. Tune it on the Advanced tab and save it here, or
          serve it once and the run shows up on its own.
        </p>
      ) : null}
      {list.map((p) => (
        <ProfileCard
          key={p.profile_id}
          p={p}
          bench={benchResult(benches.data, p.request)}
          pill={profilePill(p, s.deployments)}
          onLoad={() => {
            s.applyRequest(p.request, p)
            onAdvancedTab()
          }}
          onServe={() => {
            s.applyRequest(p.request, p)
            onServeTab()
          }}
          onStar={() => void act(backend.patchProfile(p.profile_id, { is_default: !p.is_default }))}
          onRename={(name) => void act(backend.patchProfile(p.profile_id, { name }))}
          onDelete={() => {
            if (window.confirm(`Delete the profile "${p.name}"? Its settings are not kept anywhere else.`)) {
              void act(backend.deleteProfile(p.profile_id))
            }
          }}
        />
      ))}
    </div>
  )
}

function Chips({ req }: { req: LaunchRequest }) {
  return (
    <span className="mdl-pills">
      {profileSummary(req).map((c) => (
        <span key={c} className="mdl-pill mono">
          {c}
        </span>
      ))}
    </span>
  )
}

function ProfileCard({
  p,
  bench,
  pill,
  onLoad,
  onServe,
  onStar,
  onRename,
  onDelete,
}: {
  p: Profile
  bench: string | null
  pill: ProfilePill
  onLoad: () => void
  onServe: () => void
  onStar: () => void
  onRename: (name: string) => void
  onDelete: () => void
}) {
  const now = useFitsNow(p.request)
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(p.name)
  const result = lastResultLine(p) ?? bench
  const done = () => {
    setEditing(false)
    const next = name.trim()
    if (next && next !== p.name) onRename(next)
    else setName(p.name)
  }
  return (
    <div className="mdl-hrow">
      <div className="mdl-hrow-head">
        <button
          type="button"
          className="ghost"
          aria-pressed={p.is_default}
          aria-label={p.is_default ? `Unstar ${p.name}` : `Star ${p.name} as this model's default`}
          onClick={onStar}
        >
          {p.is_default ? '★' : '☆'}
        </button>
        {editing ? (
          <>
            <input
              aria-label="Profile name"
              value={name}
              maxLength={80}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && done()}
            />
            <button type="button" className="ghost" onClick={done}>
              Done
            </button>
          </>
        ) : (
          <span className="label">{p.name}</span>
        )}
        <span className="mdl-pill">{p.is_default ? `default · ${pill}` : pill}</span>
        <span className="unit">{new Date(p.updated_at * 1000).toLocaleString()}</span>
        {now ? (
          <span className="unit">
            <Lamp signal={now.signal} label={now.label} /> {now.label}
          </span>
        ) : null}
        <span className="mdl-hrow-actions">
          <button type="button" className="ghost" onClick={onLoad}>
            Load
          </button>
          <button
            type="button"
            className="ghost"
            onClick={onServe}
            disabled={now?.wont === true}
            title={now?.wont ? 'Won’t fit right now' : undefined}
          >
            Serve
          </button>
          {!editing ? (
            <button type="button" className="ghost" onClick={() => setEditing(true)}>
              Rename
            </button>
          ) : null}
          <button type="button" className="ghost" onClick={onDelete}>
            Delete
          </button>
        </span>
      </div>
      <Chips req={p.request} />
      {result ? <span className="unit">{result}</span> : null}
    </div>
  )
}

/** "Fits right now" for a stored request: one `/api/plan` per distinct
 *  request per minute, shared by every card that shows it. */
const FITS_TTL_MS = 60_000
const fitsCache = new Map<string, { at: number; word: Promise<Word> }>()

function useFitsNow(req: LaunchRequest): (Word & { wont: boolean }) | null {
  const { backend } = useBackend()
  const key = JSON.stringify(req)
  const [word, setWord] = useState<(Word & { wont: boolean }) | null>(null)
  useEffect(() => {
    let live = true
    let hit = fitsCache.get(key)
    if (!hit || Date.now() - hit.at > FITS_TTL_MS) {
      const body = planBodyFrom(JSON.parse(key) as LaunchRequest)
      hit = {
        at: Date.now(),
        word: backend.plan(body).then(
          (result) =>
            planWord({
              running: false,
              routing: false,
              providerRuntime: false,
              variant: null,
              result,
              checking: false,
              hostPool: memoryPool((JSON.parse(key) as LaunchRequest).runtime as Runtime) === 'host',
            }),
          () => ({ label: 'Not checked', signal: 'idle' as const }),
        ),
      }
      fitsCache.set(key, hit)
    }
    void hit.word.then((w) => {
      if (!live) return
      const label =
        w.label === 'Fits' ? 'Fits right now'
        : w.label === 'Slow' ? 'Slow right now'
        : w.label === 'Won’t fit' || w.label === 'Not now' ? 'Won’t fit right now'
        : w.label
      setWord({ ...w, label, wont: w.label === 'Won’t fit' || w.label === 'Not now' })
    })
    return () => {
      live = false
    }
  }, [backend, key])
  return word
}
