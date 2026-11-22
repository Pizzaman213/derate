/** The Parallelism group's shape list and link card.
 *
 *  Every row is a shape the planner itself proposed, re-judged by the
 *  coordinator at that shape (`POST /api/plan/candidates`), so the verdict on
 *  a row is the verdict that shape would get -- never a client guess. */

import { useEffect, useId, useState } from 'react'
import type { CandidatesResponse, LinkMeasurement, PlanCandidate, Verdict } from '../../api/types'
import { Lamp } from '../../components/Lamp'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { gib } from './verdictView'
import type { ModelSession } from './useModelSession'

const KIND_LABEL: Record<string, string> = {
  single: 'Single node',
  single_node: 'Single node',
  tensor: 'Tensor parallel',
  pipeline: 'Pipeline parallel',
  expert: 'Expert parallel',
  hybrid: 'Hybrid',
}

/** The same words `planWord` uses for the same verdicts. */
function verdictWord(v: Verdict | null): { label: string; signal: 'live' | 'warn' | 'fault' } {
  if (v === 'fits') return { label: 'Fits', signal: 'live' }
  if (v === 'fits_degraded') return { label: 'Slow', signal: 'warn' }
  return { label: v === 'wont_fit' ? 'Won’t fit' : 'Refused', signal: 'fault' }
}

const sameDegrees = (c: PlanCandidate, d: ModelSession['degrees']) =>
  d != null &&
  (d.tensor_parallel ?? 1) === c.degrees.tensor_parallel &&
  (d.pipeline_parallel ?? 1) === c.degrees.pipeline_parallel &&
  (d.expert_parallel ?? 1) === c.degrees.expert_parallel &&
  (d.data_parallel ?? 1) === c.degrees.data_parallel

export function ParallelismPicker({ s, active }: { s: ModelSession; active: boolean }) {
  const { backend } = useBackend()
  const uid = useId()
  const [resp, setResp] = useState<CandidatesResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  // The base question is the plan without the operator's own shape: the list
  // answers "which shapes exist", and a pinned one would answer only itself.
  const { parallelism: _p, node_ids: _n, ...base } = s.planBody
  const key = JSON.stringify(base)

  useEffect(() => {
    if (!active) return
    let live = true
    const t = window.setTimeout(() => {
      backend
        .planCandidates(JSON.parse(key))
        .then((r) => {
          if (!live) return
          setResp(r)
          setErr(null)
        })
        .catch((e: unknown) => live && setErr(e instanceof Error ? e.message : String(e)))
    }, 600)
    return () => {
      live = false
      window.clearTimeout(t)
    }
  }, [active, key, backend])

  const plannerPick = s.degrees == null && s.nodeIds == null
  return (
    <div className="mdl-stack">
      <div className="mdl-shapes" role="radiogroup" aria-label="Parallelism shape">
        <label className="mdl-shape">
          <input
            type="radio"
            name={`${uid}-shape`}
            checked={plannerPick}
            onChange={() => {
              s.setDegrees(null)
              s.setNodeIds(null)
            }}
          />
          <span className="label">Planner pick</span>
          <span className="unit">{resp ? `currently ${KIND_LABEL[resp.recommended_kind] ?? resp.recommended_kind}` : ''}</span>
        </label>
        {(resp?.candidates ?? []).map((c) => {
          const w = verdictWord(c.verdict)
          return (
            <label key={c.kind} className="mdl-shape">
              <input
                type="radio"
                name={`${uid}-shape`}
                checked={!plannerPick && sameDegrees(c, s.degrees)}
                onChange={() => {
                  s.setDegrees({ ...c.degrees })
                  s.setNodeIds([...c.node_ids])
                }}
              />
              <span className="label">{KIND_LABEL[c.kind] ?? c.kind}</span>
              <span className="unit">{`${c.node_ids.length} node${c.node_ids.length === 1 ? '' : 's'}`}</span>
              <span className="mdl-shape-word">
                <Lamp signal={w.signal} label={w.label} />
                {w.label}
              </span>
              <span className="mono unit">{c.per_node_bytes != null ? `${gib(c.per_node_bytes)}/node` : ''}</span>
              {c.error ? <Verbatim text={c.error} size="unit" /> : null}
            </label>
          )
        })}
      </div>
      {err ? <Verbatim text={err} size="unit" /> : null}
      {active && resp == null && !err ? <p className="unit">Judging each shape…</p> : null}
    </div>
  )
}

/** What carried the measured collective between two planned nodes. */
export function LinkState({
  link,
  busy,
  error,
  onMeasure,
}: {
  link: LinkMeasurement | null
  busy: boolean
  error: string
  onMeasure: () => void
}) {
  const [signal, text] =
    link == null
      ? (['idle', 'Never measured'] as const)
      : link.method === 'tcp'
        ? (['fault', 'NCCL fell back to TCP'] as const)
        : (['live', link.gpudirect_rdma ? 'RDMA' : `measured over ${link.method}`] as const)
  return (
    <div className="mdl-link">
      <Lamp signal={signal} label={text} />
      <span className="mono unit">{link ? `${link.src} ↔ ${link.dst}: ${text}` : text}</span>
      <button type="button" className="ghost" disabled={busy} onClick={onMeasure}>
        {busy ? 'Measuring…' : 'Check again'}
      </button>
      {error ? <Verbatim text={error} size="unit" /> : null}
    </div>
  )
}
