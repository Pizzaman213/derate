// Serving a model by routing to somebody else's hardware.
//
// The other half of the Serve pane. `ServePanel` answers "run it here": plan,
// fit gate, machines, sparkrun. This answers the question that has no launch in
// it at all -- a provider already runs this model, and the only decision left is
// whether this cluster's /v1 carries its name.
//
// It is one switch, and the switch is the provider's allowlist. Everything
// downstream follows from that one edit: `ProviderService.servable()` feeds
// `Router.rebuild`, so /v1/models, routing, /api/topology and the chat picker
// all change together, with no restart and nothing else to press.

import { useState } from 'react'

import type { ProviderCatalogueModel } from '../../api/types'
import { useBackend } from '../../state/backend'
import { Lamp } from '../../components/Lamp'

/** One provider's terms for one model, whether or not it is switched on.
 *
 *  Assembled by the inspector from the two provider surfaces -- the filtered
 *  listing for what is served, the catalogue for what is merely offered -- so
 *  this component never has to know which endpoint a fact came from. */
export interface RouteTargetFacts {
  provider_id: string
  display_name: string
  /** The name this cluster's /v1 would answer to. */
  served_name: string
  /** The id the allowlist is written in terms of. Not always the served name:
   *  a provider's aliases can differ, and the allowlist is upstream ids. */
  upstream_id: string
  context_length: number | null
  input_cost_per_mtok: number | null
  output_cost_per_mtok: number | null
  supports_tools: boolean
  supports_streaming: boolean
  /** Whether this cluster is routing to it right now. */
  served: boolean
  /** The provider's own health. Only the served side carries one; `null` on a
   *  model that is merely offered, and null must not render as unhealthy. */
  healthy: boolean | null
  admission_block: string | null
  /** Whether anybody ever chose this provider's allowlist. `false` is the
   *  grandfathered record that serves everything it publishes; `null` is a
   *  port that cannot say, which is not the same as "no". */
  models_chosen: boolean | null
}

const price = (v: number | null) => (v == null ? null : `$${v.toFixed(2)}`)

export function RouteCard({
  facts,
  catalogue,
  catalogueError,
}: {
  facts: RouteTargetFacts
  /** This provider's WHOLE catalogue, each row saying whether it is on.
   *
   *  Required, and not for display: the allowlist is written as the complete
   *  set rather than a delta, so switching one model needs every other model's
   *  current state. `null` means it has not arrived -- see the guard below,
   *  which is the important part of this component. */
  catalogue: ProviderCatalogueModel[] | null
  catalogueError: string | null
}) {
  const { backend, invalidate } = useBackend()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // What the last click asked for, held locally so the switch lands instantly
  // rather than after a round trip and the catalogue's own lazy poll.
  const [pending, setPending] = useState<boolean | null>(null)

  const served = pending ?? facts.served
  const inp = price(facts.input_cost_per_mtok)
  const out = price(facts.output_cost_per_mtok)

  // The guard that matters. The allowlist is sent as the complete set, so a
  // write computed from a catalogue that failed to load would send exactly one
  // id and switch off everything else this provider serves. There is no safe
  // partial version of this edit, so without the catalogue the button is not
  // offered at all.
  const ready = catalogue != null && catalogue.length > 0

  const setServed = async (next: boolean) => {
    if (!catalogue) return
    const enabled = catalogue.filter((m) => m.enabled).map((m) => m.upstream_id)
    const wanted = next
      ? [...new Set([...enabled, facts.upstream_id])]
      : enabled.filter((id) => id !== facts.upstream_id)
    setPending(next)
    setBusy(true)
    setError(null)
    try {
      await backend.patchProvider(facts.provider_id, { enabled_models: wanted })
      invalidate()
    } catch (e) {
      setPending(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  // The grandfathered case, said before the click rather than discovered after
  // it. With no allowlist ever written this provider serves everything it
  // publishes, so switching ONE model off necessarily writes the other N-1 as
  // an explicit choice -- a real consequence of a small-looking button, and the
  // kind of thing this screen states rather than performs quietly.
  const materialises =
    facts.models_chosen === false && served && catalogue != null && catalogue.length > 1

  return (
    <div style={{ display: 'grid', gap: 'var(--s-2)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        {facts.healthy == null ? null : (
          <Lamp
            signal={facts.healthy ? 'live' : 'fault'}
            label={facts.healthy ? 'healthy' : 'unhealthy'}
          />
        )}
        <span style={{ flex: 1 }}>{facts.display_name}</span>
        {/* The string a client would send as `model`. Shown whether or not it
            is switched on, because it is what the switch is about. */}
        <span className="mono unit">{facts.served_name}</span>
      </div>

      <div className="unit">
        {[
          facts.context_length ? `${facts.context_length.toLocaleString()} context` : null,
          inp && out ? `${inp} / Mtok in · ${out} / Mtok out` : 'not priced',
          facts.supports_streaming ? 'streaming' : null,
          facts.supports_tools ? 'tools' : null,
        ]
          .filter(Boolean)
          .join(' · ')}
      </div>

      {/* What being served actually gets you, which is not always "tokens".
          Health is per PROVIDER, not per model: `targets.py` sets a target's
          `admitting` from `provider.healthy`, so a model can be switched on,
          appear in /v1/models, and still refuse every request until the
          provider recovers. A flat "Served." there is a sentence that reads as
          false the moment somebody sends a request. */}
      <div className="unit">
        {!served
          ? `Not served. ${facts.display_name} publishes this model, and nothing on ` +
            'this cluster’s API answers to it until you switch it on.'
          : facts.healthy === false
            ? `Served, but ${facts.display_name} is not answering. The name is on this ` +
              'cluster’s /v1 and a request for it is refused rather than forwarded, ' +
              'until the provider recovers — a successful refresh from Settings is ' +
              'what clears this.'
            : `Served. Requests naming ${facts.served_name} at this cluster’s /v1 are ` +
              `forwarded to ${facts.display_name} and billed there.`}
      </div>

      {/* Rate limit, budget, draining. Distinct from health, and the server's
          own sentence -- rendered as it arrived, with only the provider it is
          about put in front of it. */}
      {facts.admission_block ? (
        <div className="unit" style={{ color: 'var(--warn)' }}>
          {facts.display_name} is not admitting requests right now: {facts.admission_block}
        </div>
      ) : null}

      {materialises ? (
        <div className="unit" style={{ color: 'var(--warn)' }}>
          Nothing has ever been chosen for {facts.display_name}, so it serves every
          model it publishes. Switching this one off chooses the other{' '}
          {(catalogue!.length - 1).toLocaleString()} — that is what the rest of them
          become, rather than staying unchosen.
        </div>
      ) : null}

      {catalogueError ? (
        <div className="unit" style={{ color: 'var(--warn)', whiteSpace: 'pre-wrap' }}>
          Could not read {facts.display_name}’s catalogue, so this cannot be changed
          safely: the allowlist is written as the complete set, and one built from a
          partial read would switch off everything else. {catalogueError}
        </div>
      ) : null}

      <div>
        <button
          disabled={busy || !ready}
          onClick={() => void setServed(!served)}
          title={
            ready
              ? undefined
              : 'Waiting for this provider’s catalogue — the switch needs every ' +
                'other model’s current state to write the allowlist.'
          }
        >
          {busy
            ? served
              ? 'Serving…'
              : 'Stopping…'
            : served
              ? 'Stop serving'
              : 'Serve on the API'}
        </button>
      </div>

      {error ? (
        <div className="label" style={{ fontWeight: 400, color: 'var(--warn)', whiteSpace: 'pre-wrap' }}>
          {error}
        </div>
      ) : null}
    </div>
  )
}
