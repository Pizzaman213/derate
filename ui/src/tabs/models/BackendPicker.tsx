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
import { Select, type SelectOption } from '../../components/Select'
import { useBackend } from '../../state/backend'

export function BackendPicker({
  providerId,
  upstreamId,
}: {
  providerId: string
  upstreamId: string
}) {
  const { backend } = useBackend()
  const [rows, setRows] = useState<ProviderBackendOption[] | null>(null)
  // `null` distinguishes "never asked" from "asked, and it is genuinely
  // unsupported" -- the latter renders nothing, the former renders nothing
  // too, but only one of them is worth telling apart from a real error.
  const [unsupported, setUnsupported] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // What the last click asked for, held locally so the select reflects the
  // choice instantly rather than after a round trip -- the same reason
  // `RouteCard` holds a `pending` boolean.
  const [pending, setPending] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setRows(null)
    setUnsupported(false)
    setError(null)
    setPending(null)
    backend
      .providerBackends(providerId, upstreamId)
      .then((r) => {
        if (live) setRows(r)
      })
      .catch((e: unknown) => {
        if (!live) return
        const message = e instanceof Error ? e.message : String(e)
        // A kind with no `supports_backend_routing` answers 400 for every
        // model it serves. That is not a fetch failure worth a warning on a
        // pane that is mostly OpenAI/Together/Ollama providers -- it is the
        // ordinary answer for all of them, and the control simply does not
        // apply.
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

  if (unsupported || (!rows && !error)) return null
  if (error) {
    return (
      <div className="unit" style={{ color: 'var(--warn)', whiteSpace: 'pre-wrap' }}>
        Could not read {providerId}’s backend list: {error}
      </div>
    )
  }
  if (!rows || rows.length <= 1) return null

  const current = pending ?? rows.find((r) => r.pinned)?.tag ?? ''

  const options: SelectOption<string>[] = [
    { value: '', label: 'Automatic' },
    ...rows.map((r) => ({
      value: r.tag,
      label:
        r.input_cost_per_mtok != null && r.output_cost_per_mtok != null
          ? `${r.provider_name} — $${r.input_cost_per_mtok.toFixed(2)} / $${r.output_cost_per_mtok.toFixed(2)} per Mtok`
          : r.provider_name,
    })),
  ]

  const setPin = async (tag: string) => {
    setPending(tag)
    setBusy(true)
    setError(null)
    try {
      // A single-key patch: `backend_pins` merges into whatever this
      // provider already has pinned for its other models, so this never has
      // to know or resend them.
      await backend.patchProvider(providerId, { backend_pins: { [upstreamId]: tag || '' } })
      const fresh = await backend.providerBackends(providerId, upstreamId)
      setRows(fresh)
    } catch (e) {
      setPending(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ display: 'grid', gap: 'var(--s-1)' }}>
      <div className="sub">backend</div>
      <div className="unit">
        OpenRouter can answer {upstreamId} from any of these hosts and picks one itself unless
        pinned here.
      </div>
      <Select
        value={current}
        disabled={busy}
        options={options}
        onChange={(tag) => void setPin(tag)}
        aria-label={`Backend host for ${upstreamId}`}
      />
      {error ? (
        <div className="label" style={{ fontWeight: 400, color: 'var(--warn)', whiteSpace: 'pre-wrap' }}>
          {error}
        </div>
      ) : null}
    </div>
  )
}
