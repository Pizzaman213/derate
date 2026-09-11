import { useEffect, useState } from 'react'
import type {
  SpeculativeHead,
  SpeculativeHeads,
  SpeculativeMeasurement,
  SpeculativeOption,
} from '../../api/types'
import { useBackend } from '../../state/backend'
import { Verbatim } from '../../components/Verbatim'
import { relativeTime } from '../../format'
import type { SpecRef } from '../../state/routes'
import { Select, type SelectOption } from '../../components/Select'
import { ownerOf } from './owner'
import { OwnerMark, useAvatars } from './OwnerMark'
import {
  EXTERNAL,
  HEAD_TOKENS,
  clampTokens,
  describes,
  headLabel,
  markOwner,
  methodChoice,
  orderHeads,
  refFor,
  speedup,
} from './speculative'

/** The `?spec=` control: whether to speculate, with what, and how many tokens.
 *
 *  Nothing here computes anything. Which methods this checkpoint offers, what
 *  each one costs, how many tokens it may draft and whether it can be launched
 *  at all are decided by `resolver/speculators.py` and arrive on
 *  `PlanResponse.speculative_options`; which published head is worth using
 *  comes from `GET /api/models/speculative-heads`. Picking one writes
 *  `?spec=method:n` and the panel replans, so the verdict, the memory bars and
 *  the refusal beside this field are all a real answer to the question this
 *  field asks -- not a client-side annotation of an answer to a different one.
 *
 *  **The checkbox is unticked on arrival even when there is a recommendation.**
 *  Enabling costs memory the fit gate then charges, and a model that fits today
 *  can stop fitting -- so the estimate is shown beside the box rather than
 *  applied for you. Auto-recommend, not auto-enable.
 */
export function SpeculativeField({
  options,
  chosen,
  onChoose,
  modelId,
}: {
  options: SpeculativeOption[]
  chosen: SpecRef | null
  onChoose: (spec: SpecRef | null) => void
  /** The model heads are searched FOR. Absent, nothing is scanned — there is
   *  nothing to search against. */
  modelId?: string
}) {
  const scan = useHeadScan(modelId)
  // Once for the whole field. `OwnerMark` never subscribes itself --
  // see the note on `useAvatars`.
  useAvatars()

  // Nothing to choose, nothing chosen AND nothing found. The second clause
  // matters: a refused method leaves `result` null, so the options go empty —
  // and hiding the control then would make an override a one-way door, with no
  // value you can pick that means "off" and only the address bar to edit. Same
  // reasoning as the "Choose for me" button beside the context field.
  if (options.length === 0 && chosen === null && !scan.found?.recommended_head) {
    return null
  }

  const suggested = scan.found?.recommended_head ?? null
  // What the checkbox is describing. A live selection wins over the
  // suggestion: ticking the box applies the suggestion, but from then on the
  // row has to name what is actually going to launch.
  const active = describes(options, chosen)

  return (
    <div style={{ marginTop: 'var(--s-3)' }}>
      <label
        htmlFor="sp-on"
        style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}
      >
        <input
          id="sp-on"
          type="checkbox"
          checked={chosen !== null}
          disabled={chosen === null && !suggested}
          onChange={(e) => {
            if (!e.target.checked) return onChoose(null)
            if (suggested) onChoose(refFor(suggested))
          }}
        />
        <span className="label">Speculative decoding</span>
        <Estimate
          baseline={scan.found?.baseline_tps}
          head={chosen ? null : suggested}
          busy={scan.busy}
        />
      </label>

      {chosen ? (
        <Chosen
          chosen={chosen}
          option={active}
          onTokens={(n) => onChoose({ ...chosen, tokens: n })}
        />
      ) : suggested ? (
        <Describe head={suggested} />
      ) : null}

      {scan.found?.ngram_note ? (
        <div style={{ margin: '4px 0 0 22px' }}>
          <Verbatim text={scan.found.ngram_note} size="unit" />
        </div>
      ) : null}

      <Advanced
        options={options}
        chosen={chosen}
        onChoose={onChoose}
        scan={scan}
      />

      {active ? (
        <>
          <p
            className="label"
            style={{
              margin: '6px 0 0',
              fontWeight: 400,
              whiteSpace: 'pre-wrap',
              color: active.launchable ? undefined : 'var(--warn)',
            }}
          >
            {active.note}
          </p>
          <Measured rows={active.measured ?? []} />
        </>
      ) : chosen === null && !suggested ? (
        <p className="unit" style={{ margin: '6px 0 0' }}>
          Drafts several tokens per step and verifies them in one pass. It costs
          memory the fit gate charges above, and it only pays off when the drafts
          are accepted — which is a property of the workload, not of the model.
        </p>
      ) : null}
    </div>
  )
}

/** `up to 6.1x at n=8`, or what somebody measured, or nothing at all.
 *
 *  A multiple and never a tok/s — see `speculative.ts::speedup` for why, and
 *  for the contradiction it fixes. The k is named because the figure is the
 *  head's ceiling at its OWN maximum, which is the k the server ranked it on;
 *  lowering n below that lowers the ceiling, and the card recomputes it
 *  properly rather than this putting a second copy of `speculative_overhead`
 *  into TypeScript.
 *
 *  A measurement displaces the ceiling when there is one, and is stated
 *  against its own recorded baseline for the same reason — a sweep's tok/s is
 *  about the machine it ran on.
 */
function Estimate({
  baseline,
  head,
  busy,
}: {
  baseline?: number
  head: SpeculativeHead | null
  busy: boolean
}) {
  if (busy) return <span className="unit">looking for draft heads…</span>
  if (!head) return null
  const best = head.measured?.[0]
  const measured = best ? speedup(best.best_tps, best.baseline_tps) : null
  const ceiling = speedup(head.ceiling_tps, baseline)
  if (measured) {
    return (
      <span className="unit" style={{ marginLeft: 'auto' }}>
        {`${measured.toFixed(1)}x measured on ${best!.workload}`}
      </span>
    )
  }
  if (!ceiling) return null
  return (
    <span className="unit" style={{ marginLeft: 'auto' }}>
      {`up to ${ceiling.toFixed(1)}x at n=${head.max_tokens}`}
    </span>
  )
}

/** Where an option came from, in the words that fit what it actually is.
 *
 *  `declared_by` is a CONFIG KEY for a method the checkpoint declares
 *  (`num_nextn_predict_layers`) and an ARCHITECTURE CLASS for a published head
 *  (`LlamaForCausalLMEagle3`). One phrasing cannot serve both: "declared by
 *  LlamaForCausalLMEagle3" names no key and reads as though a config held it.
 */
function declaredBy(source?: string, key?: string): string {
  if (!key) return ''
  return source === 'head' ? ` · ${key}` : ` · declared by ${key}`
}

/** Which head, which method, and where it came from. */
function Describe({ head }: { head: SpeculativeHead }) {
  return (
    <p className="unit" style={{ margin: '2px 0 0 22px' }}>
      <OwnerMark owner={markOwner(head)} variant="inline" />{' '}
      <span className="mono" style={{ wordBreak: 'break-all' }}>
        {head.model_id}
      </span>
      {` · ${head.method}`}
      {head.draft_bytes ? ` · ${(head.draft_bytes / 1e9).toFixed(2)} GB` : ''}
      {declaredBy(head.source, head.declared_by)}
    </p>
  )
}

/** What is actually selected, plus the one number the operator can turn.
 *
 *  `n` is the drafted tokens per step -- `--speculative-config`'s
 *  `num_speculative_tokens`. It is bounded above by what the source of the
 *  option says it can draft: an MTP checkpoint carries a fixed number of heads
 *  and drafting past them means looping one, which is a claim about the runtime
 *  that nothing here has verified.
 */
function Chosen({
  chosen,
  option,
  onTokens,
}: {
  chosen: SpecRef
  option: SpeculativeOption | null
  onTokens: (n: number) => void
}) {
  const max = option?.max_tokens ?? 0
  return (
    <div style={{ margin: '2px 0 0 22px' }}>
      <p className="unit" style={{ margin: 0 }}>
        {chosen.model ? (
          <>
            <OwnerMark owner={ownerOf(chosen.model)} variant="inline" />{' '}
            <span className="mono" style={{ wordBreak: 'break-all' }}>
              {chosen.model}
            </span>
          </>
        ) : (
          <span className="mono">{chosen.method}</span>
        )}
        {chosen.model ? ` · ${chosen.method}` : ''}
        {option?.draft_bytes
          ? ` · ${(option.draft_bytes / 1e9).toFixed(2)} GB`
          : ''}
        {declaredBy(option?.source, option?.declared_by)}
      </p>
      <label
        htmlFor="sp-n"
        style={{ display: 'flex', alignItems: 'baseline', gap: 6, marginTop: 4 }}
      >
        <span className="unit">draft</span>
        <input
          id="sp-n"
          className="mono"
          type="number"
          min={1}
          max={max || undefined}
          value={chosen.tokens}
          style={{ width: 64 }}
          onChange={(e) => {
            const n = Number(e.target.value)
            if (!Number.isFinite(n)) return
            onTokens(clampTokens(n, max))
          }}
        />
        <span className="unit">
          tokens per step{max ? ` (max ${max})` : ''}
        </span>
      </label>
    </div>
  )
}

/** Everything the checkbox does not cover, folded away.
 *
 *  Open it and this is the control as it was before the checkbox existed: the
 *  method picker with ngram and any option derate found and cannot price, a
 *  field for a repository nobody has heard of, and the ranked list. Nothing was
 *  removed to make room for the recommendation.
 */
function Advanced({
  options,
  chosen,
  onChoose,
  scan,
}: {
  options: SpeculativeOption[]
  chosen: SpecRef | null
  onChoose: (spec: SpecRef | null) => void
  scan: HeadScan
}) {
  // "External" is a mode, not a method: once the coordinator answers it comes
  // back as `eagle3`/`dspark`/`mtp` -- whatever the head declared -- so the
  // picker has to stay on the external row while a head is named, rather than
  // jumping to whichever method the answer turned out to be.
  //
  // Local state, NOT read from the URL, and that is a fix rather than a
  // shortcut. Picking the external row before typing anything means
  // `model: ''`, `href` drops falsy parameters, and the reparse loses the mode
  // entirely -- so the row could be selected and would snap straight back,
  // taking the browser below with it. Whether the picker is in this mode is a
  // fact about the control; only a NAMED head is a selection worth sharing.
  const [externalMode, setExternalMode] = useState(chosen?.model != null)
  const external = externalMode || chosen?.model != null

  // An option derate found and cannot price is offered as a row that explains
  // itself rather than being filtered out. "DSpark is here and this build
  // cannot budget it" is a useful thing to read; a shorter list that silently
  // omits it is not, and it is the kind of omission somebody then spends an
  // afternoon rediscovering from a model card.
  const choices: SelectOption<string>[] = [
    { value: '', label: 'off — one token per step' },
    ...options.map((o) => ({
      value: o.launchable ? o.method : `${o.method}:blocked`,
      label: o.launchable
        ? `${o.method} — drafts ${o.default_tokens}`
        : `${o.method} — cost not derived`,
    })),
    // Always offered, and not conditional on the options above: a head is
    // published against the target and the target's config says nothing about
    // it, so there is no list here that could ever know one exists. Qwen3-Next
    // is the case that makes this necessary -- its own config declares no MTP
    // while the image loads `Qwen3NextMTP` from a separate repository.
    { value: EXTERNAL, label: 'a draft head I will name' },
  ]
  const value = external ? EXTERNAL : chosen ? chosen.method : ''
  // A method in the URL that this checkpoint does not offer is shown as
  // itself rather than snapped back to "off": the coordinator refuses it with
  // a sentence naming what IS offered, and that sentence is more useful than a
  // control that quietly disagrees with the address bar.
  const known = options.some((o) => o.method === value)
  if (value && !known && !choices.some((c) => c.value === value)) {
    choices.push({ value, label: `${value} — not offered by this model` })
  }

  return (
    <details style={{ marginTop: 'var(--s-2)' }}>
      <summary className="unit" style={{ cursor: 'pointer' }}>
        Change head
      </summary>
      <div className="fld" style={{ maxWidth: 320, marginTop: 'var(--s-2)' }}>
        <label htmlFor="sp-spec">Method</label>
        <Select
          id="sp-spec"
          value={value}
          options={choices}
          onChange={(next) => {
            // Both halves come from one place, and they are independent: a
            // mode is not a request. See `methodChoice`, and the two times
            // this control destroyed itself before it was.
            const { spec, external: mode } = methodChoice(next, options)
            setExternalMode(mode)
            onChoose(spec)
          }}
        />
      </div>
      {external ? (
        <HeadField
          value={chosen?.model ?? ''}
          onCommit={(repo) =>
            onChoose(
              repo
                ? {
                    method: chosen?.method ?? 'eagle3',
                    tokens: chosen?.tokens ?? HEAD_TOKENS,
                    model: repo,
                  }
                : null,
            )
          }
        />
      ) : null}
      <HeadBrowser scan={scan} onPick={(head) => onChoose(refFor(head))} />
    </details>
  )
}

/** The head's repository id, committed on blur or Enter rather than per key.
 *
 *  Per-keystroke would put every prefix of a repository name into the address
 *  bar and fire a hub resolution for each -- and a half-typed id is not a
 *  question the coordinator can answer, only one it can refuse. Local state
 *  until committed, so the URL carries repositories somebody meant.
 */
function HeadField({
  value,
  onCommit,
}: {
  value: string
  onCommit: (repo: string) => void
}) {
  const [text, setText] = useState(value)
  return (
    <div className="fld" style={{ maxWidth: 320, marginTop: 'var(--s-2)' }}>
      <label htmlFor="sp-head">Draft head repository</label>
      <input
        id="sp-head"
        className="mono"
        type="text"
        placeholder="AngelSlim/Qwen3-4B_eagle3"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => onCommit(text.trim())}
        onKeyDown={(e) => {
          if (e.key === 'Enter') onCommit(text.trim())
        }}
      />
    </div>
  )
}

/** What a sweep actually measured, one row per workload.
 *
 *  Renders nothing when nobody has run `python3 -m tests.spec_sweep` for this
 *  model on this hardware, which is the ordinary case — and the range above
 *  stays either way. A measurement narrows the range for the workload it was
 *  taken on; it does not replace it, because the range is still what is true
 *  for a workload nobody measured.
 *
 *  Every workload that matched is shown. Reducing three to one would mean
 *  picking a number, and the whole reason there are three is that the answer
 *  differs between them by more than the feature is worth.
 */
function Measured({ rows }: { rows: SpeculativeMeasurement[] }) {
  if (rows.length === 0) return null
  return (
    <div style={{ display: 'grid', gap: 2, marginTop: 6 }}>
      <div className="unit">measured here</div>
      {rows.map((row) => {
        const speedup = row.baseline_tps > 0 ? row.best_tps / row.baseline_tps : null
        return (
          <p
            key={`${row.workload}-${row.measured_at}`}
            className="label"
            style={{ margin: 0, fontWeight: 400, whiteSpace: 'pre-wrap' }}
          >
            <span className="mono">{row.workload}</span>
            {': '}
            {row.best_tps.toFixed(0)} tok/s at k={row.best_k}
            {speedup ? ` (${speedup.toFixed(2)}x)` : ''}
            {row.mean_acceptance != null
              ? `, acceptance ${row.mean_acceptance.toFixed(2)}`
              : ''}
            {' — '}
            {new Date(row.measured_at * 1000).toISOString().slice(0, 10)} on{' '}
            {row.gpu_name || 'this hardware'}. Your workload may differ; the
            range above still bounds it.
          </p>
        )
      })}
    </div>
  )
}

interface HeadScan {
  found: SpeculativeHeads | null
  busy: boolean
  error: string | null
  rescan: () => void
}

/** The scan, on mount rather than behind a button.
 *
 *  That is only defensible because the server writes the answer down keyed by
 *  the model and the image version: a model is scanned ONCE, not once per view,
 *  and every view afterwards is a file read. Before that store existed this had
 *  to be a button, because a dozen hub searches plus a resolve per candidate is
 *  not something to do because somebody opened a panel.
 *
 *  Guarded on the id containing a `/`: a provider model or a local directory
 *  has no hub repository to search against, and asking would be a refusal on
 *  every keystroke of a screen that never wanted one.
 */
function useHeadScan(modelId?: string): HeadScan {
  const { backend } = useBackend()
  const [found, setFound] = useState<SpeculativeHeads | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const scannable = !!modelId && modelId.includes('/')

  useEffect(() => {
    if (!scannable || !modelId) {
      setFound(null)
      return
    }
    let live = true
    setBusy(true)
    setError(null)
    setFound(null)
    backend
      .speculativeHeads(modelId)
      .then((r) => live && setFound(r))
      .catch((e: unknown) => {
        if (live) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (live) setBusy(false)
      })
    // `live` is the whole reason this is not a bare promise: switching models
    // fast enough leaves two scans in flight, and the slower one — for the
    // model you have already left — would otherwise land last and win.
    return () => {
      live = false
    }
  }, [backend, modelId, scannable])

  return {
    found,
    busy,
    error,
    rescan: () => {
      if (!modelId || !scannable) return
      setBusy(true)
      setError(null)
      backend
        .speculativeHeads(modelId, { refresh: true })
        .then(setFound)
        .catch((e: unknown) =>
          setError(e instanceof Error ? e.message : String(e)),
        )
        .finally(() => setBusy(false))
    },
  }
}

/** What the hub publishes for this model, ranked.
 *
 *  Recommendations are marked and sorted first. They are one head per METHOD
 *  FAMILY rather than the top of the ranking, because the ranking is a ceiling
 *  and within a family it ties: nine EAGLE3 checkpoints of one training run
 *  score identically. The server's own `caveat` says so and is rendered
 *  verbatim underneath, because a ceiling read as a prediction is the one way
 *  this screen could mislead.
 */
function HeadBrowser({
  scan,
  onPick,
}: {
  scan: HeadScan
  onPick: (head: SpeculativeHead) => void
}) {
  const { found, busy, error, rescan } = scan
  if (error) {
    return (
      <p className="label" style={{ margin: '6px 0 0', fontWeight: 400, color: 'var(--warn)' }}>
        {error}
      </p>
    )
  }
  if (!found) {
    return (
      <p className="unit" style={{ margin: '6px 0 0' }}>
        {busy ? 'Searching the hub…' : 'Nothing has been scanned for this model.'}
      </p>
    )
  }
  if (found.note || found.heads.length === 0) {
    return (
      <p className="unit" style={{ margin: '6px 0 0' }}>
        {found.note ?? `Nothing published for ${found.model_id} that this runtime can load.`}
        {found.rejected_total
          ? ` ${found.rejected_total} candidate(s) were found and refused.`
          : ''}
      </p>
    )
  }

  const ordered = orderHeads(found.heads)
  return (
    <div style={{ marginTop: 'var(--s-2)', display: 'grid', gap: 4 }}>
      <div className="unit">
        {found.heads.length} usable
        {found.rejected_total ? `, ${found.rejected_total} refused` : ''}
        {' · ranked by ceiling, against one another'}
        {/* A stored scan is served instantly and can be a week old, so it says
            how old. `Rescan the hub` below is what acts on that. */}
        {found.scanned_at ? ` · scanned ${relativeTime(found.scanned_at)}` : ''}
      </div>
      <div
        role="listbox"
        aria-label="Published draft heads"
        style={{
          display: 'grid',
          maxHeight: 220,
          overflowY: 'auto',
          border: '1px solid var(--rule)',
          borderRadius: 'var(--radius)',
        }}
      >
        {ordered.map((head) => (
          <button
            key={`${head.model_id}:${head.method}`}
            className="nboard-row"
            role="option"
            aria-selected={false}
            style={{
              display: 'flex',
              alignItems: 'baseline',
              gap: 8,
              textAlign: 'left',
              border: 0,
              flexWrap: 'wrap',
            }}
            onClick={() => onPick(head)}
          >
            {head.recommended ? <span className="pill">try this</span> : null}
            {/* `markOwner`, not `ownerOf`: ngram's row carries the TARGET's
                id, so the plain split would badge a string-matching heuristic
                with Qwen's logo. */}
            <OwnerMark owner={markOwner(head)} variant="inline" reserve />
            <span className="mono" style={{ wordBreak: 'break-all' }}>
              {headLabel(head)}
            </span>
            {head.source === 'method' ? null : (
              <span className="unit">{head.method}</span>
            )}
            <span className="num">
              {head.draft_bytes ? `${(head.draft_bytes / 1e9).toFixed(2)} GB` : '—'}
            </span>
            <span className="unit">
              {(() => {
                const x = speedup(head.ceiling_tps, found.baseline_tps)
                return x ? `up to ${x.toFixed(1)}x` : `ceiling ${head.ceiling_tps.toFixed(0)}`
              })()}
            </span>
          </button>
        ))}
      </div>
      {found.caveat ? <Verbatim text={found.caveat} size="label" /> : null}
      <Refused rows={found.rejected ?? []} total={found.rejected_total ?? 0} />
      <button className="ghost" disabled={busy} onClick={rescan}>
        {busy ? 'Searching the hub…' : 'Rescan the hub'}
      </button>
    </div>
  )
}


/** Why the candidates that did not make the list did not make it.
 *
 *  The server has always sent these WITH their reasons and the screen showed
 *  only a count — and the count is the one thing the reasons cannot be
 *  reconstructed from. "The runtime image does not register
 *  `Eagle3DraftModel`" tells an operator to pull a newer image; "expects a
 *  248320-token vocabulary" tells them they found a head for the next
 *  generation of the model. A bare `16 refused` tells them nothing and reads
 *  as though the hub is mostly junk.
 *
 *  Folded away, because on a good model this is longer than the list it
 *  explains. Verbatim inside, because these are resolver refusal strings and
 *  those are the product.
 */
function Refused({
  rows,
  total,
}: {
  rows: { model_id: string; reason: string }[]
  total: number
}) {
  if (rows.length === 0) return null
  return (
    <details>
      <summary className="unit" style={{ cursor: 'pointer' }}>
        why {total} {total === 1 ? 'was' : 'were'} refused
      </summary>
      <div style={{ display: 'grid', gap: 4, marginTop: 4 }}>
        {rows.map((row) => (
          <p key={row.model_id} className="unit" style={{ margin: 0 }}>
            <span className="mono" style={{ wordBreak: 'break-all' }}>
              {row.model_id}
            </span>
            {' — '}
            {row.reason}
          </p>
        ))}
        {/* The endpoint caps the list it sends. Saying so beats a list that
            silently stops -- a truncated set read as complete is how somebody
            concludes a head is fine when it was simply never shown. */}
        {total > rows.length ? (
          <p className="unit" style={{ margin: 0 }}>
            …and {total - rows.length} more not listed.
          </p>
        ) : null}
      </div>
    </details>
  )
}
