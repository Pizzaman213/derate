// OpenRouter's own backend-host choice, for one model.
//
// `RouteCard` answers "does this cluster route to this provider at all". This
// answers a narrower question that only OpenRouter can even ask: OpenRouter
// itself multiplexes one model id over several backend hosts (Anthropic
// direct, Bedrock, Vertex...) and picks one per request unless told
// otherwise. This is that "unless told otherwise" -- a live list of the
// backends OpenRouter's own endpoints call reports for this model, and a
// pin that rides along on every request from here on
// (`ProviderService._prepare` injects `provider: {only: [tag]}`).
//
// Self-contained, unlike `RouteCard`: there is no shared catalogue this needs
// to read alongside other models, so it fetches its own one row of state.

import { useEffect, useState } from 'react'

import type { ProviderBackendOption } from '../../api/types'
import { HostTable, perMtok } from '../../components/HostTable'
import { useBackend } from '../../state/backend'

export function BackendPicker({
  providerId,
  upstreamId,
  fallback,
}: {
  providerId: string
  upstreamId: string
  /** The model's `backend_fallback`, from the provider catalogue. */
  fallback: boolean
}) {
  const { backend } = useBackend()
  const [rows, setRows] = useState<ProviderBackendOption[] | null>(null)
  // A kind with no `supports_backend_routing` answers 400 for every model it
  // serves: the control simply does not apply, so nothing renders.
  const [unsupported, setUnsupported] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // What the last click asked for, held locally so the table reflects the
  // choice instantly rather than after a round trip -- the same reason
  // `RouteCard` holds a `pending` boolean. `undefined` = nothing pending.
  const [pending, setPending] = useState<string | null | undefined>(undefined)
  const [pendingFallback, setPendingFallback] = useState<boolean | null>(null)

  useEffect(() => {
    let live = true
    setRows(null)
    setUnsupported(false)
    setError(null)
    setPending(undefined)
    setPendingFallback(null)
    backend
      .providerBackends(providerId, upstreamId)
      .then((r) => {
        if (live) setRows(r)
      })
      .catch((e: unknown) => {
        if (!live) return
        const message = e instanceof Error ? e.message : String(e)
        if (/backend_routing_unsupported|does not aggregate/.test(message)) {
          setUnsupported(true)
        } else {
          setError(message)
        }
      })
    return () => {
      live = false
    }
  }, [backend, providerId, upstreamId])

  if (unsupported) return null

  const pinned = pending !== undefined ? pending : (rows?.find((r) => r.pinned)?.tag ?? null)
  const host = pinned ? rows?.find((r) => r.tag === pinned) : undefined

  // Single-key patches: `backend_pins`/`backend_fallbacks` merge into whatever
  // this provider already has for its other models.
  const write = async (patch: Parameters<typeof backend.patchProvider>[1], undo: () => void) => {
    setBusy(true)
    setError(null)
    try {
      await backend.patchProvider(providerId, patch)
      setRows(await backend.providerBackends(providerId, upstreamId))
    } catch (e) {
      undo()
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ display: 'grid', gap: 'var(--space-4)' }}>
      {host ? (
        <div className="unit">
          host: {host.provider_name} · {perMtok(host.input_cost_per_mtok)} / {perMtok(host.output_cost_per_mtok)}
        </div>
      ) : null}
      <div className="sub">backend</div>
      <div className="unit">
        OpenRouter can answer {upstreamId} from any of these hosts and picks one itself unless
        pinned here.
      </div>
      <HostTable
        rows={rows ?? []}
        pinned={pinned}
        fallback={pinned != null && (pendingFallback ?? fallback)}
        loading={rows == null && error == null}
        error={error}
        disabled={busy}
        onPin={(tag) => {
          setPending(tag)
          void write({ backend_pins: { [upstreamId]: tag ?? '' } }, () => setPending(undefined))
        }}
        onFallback={(v) => {
          setPendingFallback(v)
          void write({ backend_fallbacks: { [upstreamId]: v } }, () => setPendingFallback(null))
        }}
      />
    </div>
  )
}
