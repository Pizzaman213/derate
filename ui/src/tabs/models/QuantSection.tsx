import { useId, useMemo, useState } from 'react'
import type { QuantVariant } from '../../api/types'
import { Lamp } from '../../components/Lamp'
import { Verbatim } from '../../components/Verbatim'
import { servesOnCluster } from '../../state/runtime'
import { fitLamp } from './ladder'
import { partitionForRuntime, variantKey } from './ollamaTarget'
import { OwnerMark } from './OwnerMark'
import { ownerOf } from './owner'
import {
  formatOf,
  isUnslothDynamic,
  recommendedPicks,
  tierOf,
  TIER_ORDER,
  type Tier,
} from './quantPicks'
import type { ModelSession } from './useModelSession'
import { gib } from './verdictView'

type Format = 'all' | 'safetensors' | 'gguf'
type Tab = 'recommended' | Tier

const TIER_LABEL: Record<Tier, string> = {
  '16': '16-bit',
  '8': '8-bit',
  '6': '6-bit',
  '5': '5-bit',
  '4': '4-bit',
  '3': '3-bit',
  '2': '2-bit',
  '1': '1-bit',
  other: 'Other',
}

const SIGNAL_RANK = { live: 3, warn: 2, fault: 1, idle: 0 } as const

/** Which quantization to serve, chosen from what the ladder published.
 *
 *  Every verdict on a row is the gateway's, and the order inside a tier is the
 *  gateway's `rank` -- re-sorting in the browser would be a second answer to
 *  its question, and the one that disagreed would be the one that misled.
 *  Selecting a row does not launch anything: it decides what the verdict box
 *  above is taken for, and the plan re-runs for it. */
export function QuantSection({ s }: { s: ModelSession }) {
  const { ladder, runtime, variant } = s
  const onCluster = servesOnCluster(runtime)
  // Two panes can be mounted at once (the tab and the sheet), so radio names
  // and ids are scoped per instance or one pane's radios join the other's group.
  const uid = useId()
  const [format, setFormat] = useState<Format>('all')
  // Open on the tier the selected variant lives in when it is not one of the
  // picks -- a quant named on a pasted link should be visible, selected.
  const [tabChosen, setTab] = useState<Tab | null>(null)

  const { servableKeys, picks, formats, byTier } = useMemo(() => {
    if (!ladder) return { servableKeys: new Set<string>(), picks: [], formats: [], byTier: new Map<Tier, QuantVariant[]>() }
    const keys = new Set(partitionForRuntime(ladder.variants, runtime).servable.map(variantKey))
    const fmts = new Set(ladder.variants.map(formatOf))
    const tiers = new Map<Tier, QuantVariant[]>()
    for (const v of ladder.variants) {
      if (format !== 'all' && formatOf(v) !== format) continue
      const t = tierOf(v)
      const list = tiers.get(t)
      if (list) list.push(v)
      else tiers.set(t, [v])
    }
    return {
      servableKeys: keys,
      picks: recommendedPicks(ladder, runtime),
      formats: [...fmts],
      byTier: tiers,
    }
  }, [ladder, runtime, format])

  if (s.ladderError) {
    return (
      <section className="mdl-quant" aria-label="Quantization">
        <Verbatim text={s.ladderError} size="label" />
      </section>
    )
  }
  if (s.loadingLadder || !ladder) {
    return (
      <section className="mdl-quant" aria-label="Quantization" aria-busy="true">
        <p className="unit">Reading the quantizations on the Hub…</p>
      </section>
    )
  }
  if (!ladder.variants.length) {
    return (
      <section className="mdl-quant" aria-label="Quantization">
        <p className="unit">No other quantization was found.</p>
      </section>
    )
  }

  const selectedKey = variant ? variantKey(variant) : null
  const pickedTier = variant && !picks.some((p) => variantKey(p.variant) === selectedKey) ? tierOf(variant) : null
  const tab: Tab = tabChosen ?? pickedTier ?? 'recommended'
  const isRecommended = (v: QuantVariant) =>
    ladder.recommended != null &&
    v.repo_id === ladder.recommended.repo_id &&
    v.label === ladder.recommended.label

  const tierRows = tab === 'recommended' ? [] : (byTier.get(tab) ?? [])

  return (
    <section className="mdl-quant" aria-label="Quantization">
      <div className="mdl-quant-head">
        <span className="label">Quantization</span>
        <span className="unit">{`${ladder.variants.length} options on the Hub`}</span>
        {formats.length > 1 ? (
          <span className="mdl-seg" role="group" aria-label="File format">
            {(['all', ...(['safetensors', 'gguf'] as const).filter((f) => formats.includes(f))] as Format[]).map((f) => (
              <button key={f} type="button" aria-pressed={format === f} onClick={() => setFormat(f)}>
                {f === 'all' ? 'All' : f === 'gguf' ? 'GGUF' : 'Safetensors'}
              </button>
            ))}
          </span>
        ) : null}
      </div>

      <div className="mdl-tiers" role="tablist" aria-label="Precision">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'recommended'}
          onClick={() => setTab('recommended')}
        >
          Recommended
        </button>
        {TIER_ORDER.filter((t) => byTier.has(t)).map((t) => {
          const rows = byTier.get(t)!
          let best: 'live' | 'warn' | 'fault' | 'idle' = 'idle'
          for (const v of rows) {
            const sig = fitLamp(v, onCluster).signal
            if (SIGNAL_RANK[sig] > SIGNAL_RANK[best]) best = sig
          }
          return (
            <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
              <Lamp signal={best} hollow={best === 'idle'} size={6} label={`best in ${TIER_LABEL[t]}`} />
              {TIER_LABEL[t]}
              <span className="unit">{rows.length}</span>
            </button>
          )
        })}
      </div>

      {tab === 'recommended' ? (
        <div className="mdl-picks">
          {picks.map((p) => {
            const key = variantKey(p.variant)
            const lamp = fitLamp(p.variant, onCluster)
            return (
              <button
                key={key}
                type="button"
                className="mdl-pick"
                aria-pressed={key === selectedKey}
                onClick={() => s.selectVariant(key)}
              >
                <span className="mdl-pick-title">{p.title}</span>
                <span className="mdl-pick-label mono">
                  {isUnslothDynamic(p.variant) ? <OwnerMark owner="unsloth" variant="inline" /> : null}
                  {p.variant.label}
                </span>
                <span className="unit">{`${ownerOf(p.variant.repo_id)} · ${gib(p.variant.file_bytes)}`}</span>
                <span className="mdl-pick-fit unit">
                  <Lamp signal={lamp.signal} hollow={lamp.signal === 'idle'} label={lamp.label} />
                  {lamp.label}
                </span>
                <span className="unit mdl-pick-why">{p.why}</span>
              </button>
            )
          })}
        </div>
      ) : tierRows.length === 0 ? (
        <p className="unit">{`No ${format === 'all' ? '' : format === 'gguf' ? 'GGUF ' : 'safetensors '}quants at this precision.`}</p>
      ) : (
        <div className="mdl-qlist" role="radiogroup" aria-label={`${TIER_LABEL[tab]} quantizations`}>
          {tierRows.map((v) => {
            const key = variantKey(v)
            const lamp = fitLamp(v, onCluster)
            const loadable = servableKeys.has(key)
            const id = `${uid}-q-${key}`
            return (
              <label key={key} htmlFor={id} className={`mdl-qrow${loadable ? '' : ' unloadable'}`}>
                <input
                  id={id}
                  type="radio"
                  name={`${uid}-quant`}
                  checked={key === selectedKey}
                  onChange={() => s.selectVariant(key)}
                />
                <span className="mdl-qrow-main">
                  <span className="mdl-qrow-line">
                    <span className="mono">{v.label}</span>
                    {isUnslothDynamic(v) ? <span className="mdl-pill">Dynamic</span> : null}
                    {isRecommended(v) ? <span className="mdl-pill">best fit</span> : null}
                  </span>
                  <span className="unit" title={v.repo_id}>
                    {`${ownerOf(v.repo_id)} · ${formatOf(v) === 'gguf' ? 'GGUF' : 'safetensors'}`}
                    {loadable ? '' : ` · not loadable by ${runtime}`}
                  </span>
                </span>
                <span className="mono unit">{gib(v.file_bytes)}</span>
                <span className="mdl-qrow-fit unit">
                  <Lamp signal={lamp.signal} hollow={lamp.signal === 'idle'} label={lamp.label} />
                  {lamp.label}
                </span>
              </label>
            )
          })}
        </div>
      )}

      {/* Not on disk yet, so the first launch pulls it. Stated from the
          measured file size rather than a transfer nobody is watching: the
          control plane does not download weights, the runtime container does. */}
      {onCluster &&
      variant &&
      variant.launchable &&
      variant.file_bytes != null &&
      s.cache.complete(variant.repo_id, variant.file_bytes) !== true ? (
        <p className="unit mdl-weights-note">
          {s.cache.nodes(variant.repo_id).length
            ? `Only part of this is cached — the first launch pulls the rest of ${gib(variant.file_bytes)}.`
            : `Not cached on any node — the first launch pulls ${gib(variant.file_bytes)}.`}
        </p>
      ) : null}

      {/* Not a footnote anyone should be able to miss: the ladder is built from
          repository and file names, which is the only signal most quantizers
          leave. */}
      <p className="unit mdl-weights-note">{ladder.note}</p>
    </section>
  )
}
