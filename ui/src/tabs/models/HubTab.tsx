import { Icon } from '../../components/Icon'
import { Lamp } from '../../components/Lamp'
import { Verbatim, VerbatimList } from '../../components/Verbatim'
import { ENDPOINT_FOR_MODALITY, isAudio } from '../../api/types'
import { gbytes } from '../../format'
import { servesOnCluster } from '../../state/runtime'
import { fitLamp } from './ladder'
import { variantKey } from './ollamaTarget'
import { OwnerMark } from './OwnerMark'
import { ownerOf } from './owner'
import { ParamBreakdownTable } from './ParamBreakdownTable'
import { QuantTableCard } from './QuantTableCard'
import { repoGroups } from './quantPicks'
import type { ModelRow } from './rows'
import type { ModelSession } from './useModelSession'
import { formatParams, gib } from './verdictView'

/** One line per distinct reason, naming the machines that were not asked.
 *
 *  Grouped rather than listed per node: today every skip shares one reason, and
 *  three identical sentences under a heading read as three problems. The server
 *  owns the wording -- this only decides how many times to say it. */
function skippedLines(skipped: { node_id: string; reason: string }[] | undefined): string[] {
  if (!skipped?.length) return []
  const byReason = new Map<string, string[]>()
  for (const sk of skipped) {
    const ids = byReason.get(sk.reason) ?? []
    ids.push(sk.node_id)
    byReason.set(sk.reason, ids)
  }
  return [...byReason].map(([reason, ids]) => {
    const noun = ids.length === 1 ? 'node' : 'nodes'
    return `${ids.length} ${noun} not considered: ${ids.join(', ')} — ${reason}`
  })
}

/** What the Hub says about this model, and every repository that publishes a
 *  build of it. Reference, one tab away from serving rather than in its way. */
export function HubTab({
  s,
  modelId,
  row,
  onServeTab,
}: {
  s: ModelSession
  modelId: string
  row: ModelRow | null
  onServeTab: () => void
}) {
  const { detail, detailError, ladder } = s
  const onCluster = servesOnCluster(s.runtime)
  const groups = ladder ? repoGroups(ladder, s.runtime, s.formats) : []

  // `detail.hub` is the hub's own answer for this repo; the search row is the
  // fallback when the coordinator could not read it (hub is null).
  const hub = detail?.hub ?? null
  const downloads = hub?.downloads ?? row?.downloads ?? null
  const likes = hub?.likes ?? row?.likes ?? null
  const gated = hub?.gated ?? row?.gated ?? null
  const params = detail?.shape.total_params ?? row?.total_params ?? null
  const stats: { label: string; value: string }[] = []
  if (downloads != null) stats.push({ label: 'downloads', value: downloads.toLocaleString() })
  if (likes != null) stats.push({ label: 'likes', value: likes.toLocaleString() })
  if (hub?.last_modified) stats.push({ label: 'updated', value: new Date(hub.last_modified).toLocaleDateString() })
  if (hub?.license) stats.push({ label: 'license', value: hub.license })
  if (hub?.pipeline_tag) stats.push({ label: 'task', value: hub.pipeline_tag })
  if (params) stats.push({ label: 'parameters', value: formatParams(params) })
  if (gated != null && gated !== false) stats.push({ label: 'gated', value: String(gated) })
  if (detail) {
    stats.push({
      label: 'architecture',
      value: detail.architectures.join(', ') || detail.model_type || 'unknown',
    })
    stats.push({ label: 'modality', value: detail.modality ?? 'text' })
    stats.push({ label: 'weights', value: gib(detail.weight_bytes_effective) })
  }

  return (
    <div className="mdl-hubtab">
      {stats.length ? (
        <dl className="mdl-stats">
          {stats.map((st) => (
            <div key={st.label}>
              <dt className="unit">{st.label}</dt>
              <dd className="mono">{st.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {detail ? (
        <p>
          <a
            className="mdl-extlink"
            href={`https://huggingface.co/${modelId}`}
            target="_blank"
            rel="noopener"
          >
            Open on huggingface.co <Icon name="external-link" size={12} decorative />
          </a>
        </p>
      ) : null}

      {groups.length ? (
        <>
          <div className="sub">repos on the hub</div>
          <div className="mdl-repos">
            {groups.map((g) => {
              const lamp = fitLamp(g.pick, onCluster)
              const owner = ownerOf(g.repo)
              return (
                <div key={g.repo} className="mdl-repo">
                  <OwnerMark owner={owner} variant="inline" reserve />
                  <span className="mdl-repo-main">
                    <a
                      className="mono"
                      href={`https://huggingface.co/${g.repo}`}
                      target="_blank"
                      rel="noopener"
                    >
                      {g.repo}
                    </a>
                    <span className="unit">
                      {g.repo === ladder?.model_id ? 'Original · ' : owner === 'unsloth' ? 'Unsloth · ' : ''}
                      {`${g.variants.length} ${g.variants.length === 1 ? 'quant' : 'quants'}, ${gib(g.minBytes)} to ${gib(g.maxBytes)}`}
                    </span>
                  </span>
                  <span className="mdl-repo-fit unit">
                    <Lamp signal={lamp.signal} hollow={lamp.signal === 'idle'} label={lamp.label} />
                    {`${lamp.label} at ${g.pick.label}`}
                  </span>
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => {
                      s.selectVariant(variantKey(g.pick))
                      onServeTab()
                    }}
                  >
                    Use
                  </button>
                </div>
              )
            })}
          </div>
        </>
      ) : null}

      {detail ? (
        <>
          {/* The route, and only when it is not the one everything else
              answers on: a speech model NOT saying `/v1/audio/speech` is the
              one thing a reader cannot guess. */}
          {isAudio(detail.modality) ? (
            <p className="unit">
              answers on <span className="mono">POST {ENDPOINT_FOR_MODALITY[detail.modality!]}</span>
            </p>
          ) : null}

          <div className="sub">what this repository is</div>
          <ParamBreakdownTable detail={detail} />

          <div className="sub">provenance</div>
          <div className="row">
            <span>Parameter count</span>
            <span className="unit">{detail.param_source ?? '—'}</span>
          </div>
          <div className="row">
            <span>Quantization</span>
            <span className="unit">{detail.quant_source ?? '—'}</span>
          </div>
          <div className="row">
            <span>Weights on disk</span>
            <span className="mono">
              {detail.weight_bytes_effective ? `${gbytes(detail.weight_bytes_effective)} GiB` : '—'}
            </span>
          </div>
          {detail.from_cache ? <p className="unit">Resolved from cache.</p> : null}

          <div className="sub">runtimes</div>
          {(detail.support?.runtimes ?? []).map((r) => (
            <div key={r.runtime} className="mdl-runtime">
              <Lamp
                signal={r.level === 'supported' ? 'live' : r.level === 'unsupported' ? 'fault' : 'idle'}
                label={`${r.runtime}: ${r.level}`}
              />
              <span className="label mdl-runtime-name">{r.runtime}</span>
              {r.version ? (
                <span className="mono unit" title="the image version this was checked against">
                  {r.version}
                </span>
              ) : null}
              {/* Verbatim: the runtime's own sentence is more precise than a
                  rewrite, and it is what a refused launch would have said. */}
              <Verbatim text={r.reason} size="label" />
            </div>
          ))}

          {detail.nodes.problems.length || detail.nodes.skipped?.length ? (
            <>
              <div className="sub">on this hardware</div>
              <VerbatimList items={detail.nodes.problems} />
              {skippedLines(detail.nodes.skipped).map((line) => (
                // Muted, and deliberately not a VerbatimList row: these
                // machines were not asked, which is not the same as objecting.
                <p key={line} className="unit">
                  {line}
                </p>
              ))}
            </>
          ) : null}

          {detail.warnings.length ? (
            <>
              <div className="sub">what had to be assumed</div>
              <VerbatimList items={detail.warnings} />
            </>
          ) : null}
        </>
      ) : detailError ? (
        <>
          <div className="sub">what this repository is</div>
          {/* The resolve failing does not stop the Serve tab from being
              useful, so this is stated where the detail would have been. */}
          <p className="mdl-error label">{detailError}</p>
        </>
      ) : (
        <p className="unit">Resolving…</p>
      )}

      <QuantTableCard />
    </div>
  )
}
