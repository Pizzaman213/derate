import type { NodeStateDTO, Provider } from '../../api/types'
import { Lamp } from '../../components/Lamp'

/** Which machine a pull lands on.
 *
 *  What `NodeBoard` is for a cluster launch, this is for a pull: the same
 *  shape of choice, against a different set of machines. It reads facts and
 *  computes nothing -- in particular it does not size anything.
 *
 *  The temptation is to show free memory per row, since the pull is gated on
 *  it. Resisted deliberately. That figure comes from matching the provider's
 *  URL against the roster by exact address (`_provider_host_memory`), and
 *  reproducing that join here would be a second implementation of the gate's
 *  denominator -- the one that disagreed would be the one that let somebody
 *  fill a disk. The server already says what it weighed, in the reply, and
 *  that is where the number is shown.
 */
export function ProviderPicker({
  targets,
  chosen,
  onChoose,
  nodes,
}: {
  targets: Provider[]
  chosen: string
  onChoose: (providerId: string) => void
  /** The roster, only to name the GPU-less machines in the empty state. */
  nodes?: NodeStateDTO[]
}) {
  if (!targets.length) {
    // Same reasoning as PullCard's empty state, and the same case: the panel
    // above this has just said the machine cannot carry a rank. A control that
    // vanishes leaves that as the last word, so it says what to do instead.
    const gpuless = (nodes ?? []).filter((n) => n.profile.gpu_count === 0)
    return (
      <div className="fld">
        <label>Serving on</label>
        <div className="unit">
          {gpuless.length
            ? `No provider is configured to pull onto. ${gpuless
                .map((n) => n.profile.node_id)
                .join(', ')} ${gpuless.length === 1 ? 'has' : 'have'} no GPU and
               cannot carry a rank, but a machine like that can still serve a small
               model over the network. Run Ollama on it, add it under Settings →
               Providers, and it becomes a target here.`
            : `No provider is configured to pull onto. Run Ollama on a machine and add
               it under Settings → Providers, and it becomes a target here.`}
        </div>
      </div>
    )
  }

  return (
    <div className="fld">
      <label>Serving on</label>
      <div style={{ display: 'grid', gap: 4 }}>
        {targets.map((p) => {
          const id = `pp-${p.provider_id}`
          return (
            <div key={p.provider_id} className="nboard-row">
              <input
                id={id}
                type="radio"
                name="pull-target"
                checked={p.provider_id === chosen}
                onChange={() => onChoose(p.provider_id)}
              />
              <label htmlFor={id}>
                <span style={{ display: 'grid', gap: 2 }}>
                  <span>
                    <span className="mono">{p.display_name}</span>{' '}
                    {/* Hollow when the last refresh failed: the box is still a
                        legitimate target -- a pull may be exactly what fixes an
                        empty catalogue -- so this reports rather than blocks. */}
                    <Lamp
                      signal={p.healthy ? 'live' : 'warn'}
                      hollow={!p.healthy}
                      label={p.healthy ? 'answering' : 'not answering'}
                    />
                  </span>
                  <span className="unit">
                    {p.models.length
                      ? `${p.models.length} model${p.models.length === 1 ? '' : 's'} on it`
                      : 'nothing on it yet'}
                  </span>
                </span>
              </label>
            </div>
          )
        })}
      </div>
    </div>
  )
}
