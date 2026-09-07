import { useEffect, useRef, useState } from 'react'
import type { ModelDetail, VariantLadder } from '../../api/types'
import { useBackend } from '../../state/backend'
import { Lamp } from '../../components/Lamp'
import { Verbatim, VerbatimList } from '../../components/Verbatim'
import { CapabilityChips } from './CapabilityChips'
import { QuantLadder } from './QuantLadder'
import { ParamBreakdownTable } from './ParamBreakdownTable'
import { gbytes } from '../../format'

/** One model, in the sheet: what it is, then what you can get it as.
 *
 *  Fetches its own two payloads rather than receiving them, because both are
 *  hub-bound and neither belongs in `resources.ts` -- `useResource` refires on
 *  every `revision` bump, so an open model would re-run its hub calls after
 *  every launch, admit and settings change.
 *
 *  The two loads are deliberately separate and shown separately. Detail is one
 *  resolve and usually cached; the ladder is a search plus a repository read
 *  per GGUF repo and can take seconds. Waiting for the slow one before drawing
 *  the fast one would leave the sheet blank for no reason. */
export function ModelInspector({
  modelId,
  context,
  concurrency,
  onClose,
}: {
  modelId: string
  context: number
  concurrency: number
  onClose: () => void
}) {
  const { backend } = useBackend()
  const [detail, setDetail] = useState<ModelDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [ladder, setLadder] = useState<VariantLadder | null>(null)
  const [ladderError, setLadderError] = useState<string | null>(null)
  const [loadingLadder, setLoadingLadder] = useState(true)

  // One sequence per mount, bumped on every model change, so a response for a
  // model the sheet no longer shows cannot land -- the pattern PlannerBar
  // documents, and the reason its early-return path bumps too.
  const seq = useRef(0)

  useEffect(() => {
    const mine = ++seq.current
    setDetail(null)
    setDetailError(null)
    setLadder(null)
    setLadderError(null)
    setLoadingLadder(true)

    backend
      .modelDetail(modelId)
      .then((d) => {
        if (seq.current === mine) setDetail(d)
      })
      .catch((e: unknown) => {
        if (seq.current === mine) {
          setDetailError(e instanceof Error ? e.message : String(e))
        }
      })

    backend
      .modelVariants(modelId, { context, concurrency })
      .then((l) => {
        if (seq.current === mine) setLadder(l)
      })
      .catch((e: unknown) => {
        if (seq.current === mine) {
          setLadderError(e instanceof Error ? e.message : String(e))
        }
      })
      .finally(() => {
        if (seq.current === mine) setLoadingLadder(false)
      })
  }, [backend, modelId, context, concurrency])

  return (
    <>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          gap: 10,
          marginBottom: 4,
        }}
      >
        <h3 className="mono" style={{ flex: 1, wordBreak: 'break-all' }}>
          {modelId}
        </h3>
        <button className="ghost" onClick={onClose} aria-label="Close">
          Close
        </button>
      </div>

      {detail ? (
        <>
          <div className="unit" style={{ marginBottom: 10 }}>
            {detail.architectures.join(', ') || detail.model_type || 'architecture unknown'}
            {detail.from_cache ? ' · from cache' : null}
          </div>
          <CapabilityChips detail={detail} />

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
              {detail.weight_bytes_effective
                ? `${gbytes(detail.weight_bytes_effective)} GiB`
                : '—'}
            </span>
          </div>

          <div className="sub">runtimes</div>
          {(detail.support?.runtimes ?? []).map((r) => (
            <div
              key={r.runtime}
              style={{ display: 'flex', gap: 8, alignItems: 'baseline', padding: '3px 0' }}
            >
              <Lamp
                signal={
                  r.level === 'supported'
                    ? 'live'
                    : r.level === 'unsupported'
                      ? 'fault'
                      : 'idle'
                }
                label={`${r.runtime}: ${r.level}`}
              />
              <span className="label" style={{ width: 58 }}>
                {r.runtime}
              </span>
              {/* Verbatim: the runtime's own sentence is more precise than a
                  rewrite, and it is what a refused launch would have said. */}
              <Verbatim text={r.reason} size="label" />
            </div>
          ))}

          {detail.nodes.problems.length ? (
            <>
              <div className="sub">on this hardware</div>
              <VerbatimList items={detail.nodes.problems} />
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
        <p
          className="label"
          style={{ fontWeight: 400, color: 'var(--fault)', whiteSpace: 'pre-wrap' }}
        >
          {detailError}
        </p>
      ) : (
        <p className="unit">Resolving…</p>
      )}

      <div className="sub">quantizations</div>
      <QuantLadder
        ladder={ladder}
        loading={loadingLadder}
        error={ladderError}
        context={context}
        concurrency={concurrency}
      />
    </>
  )
}
