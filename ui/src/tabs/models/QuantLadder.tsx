import { useState } from 'react'
import type { QuantVariant, VariantLadder } from '../../api/types'
import { useBackend } from '../../state/backend'
import { Disclosure } from '../../components/Panel'
import { Lamp } from '../../components/Lamp'
import { Verbatim } from '../../components/Verbatim'
import { gbytes } from '../../format'

/** One quantization, already chosen, with the rest a click away.
 *
 *  A repository can publish forty of these, and a table of forty rows asks a
 *  question most people cannot answer and should not have to. So this opens on
 *  a single pick -- the largest variant that both fits and can be served here
 *  -- with the button already beside it. The full list is one toggle away and
 *  says how many rows it holds, because a collapsed list that hides its size
 *  reads as a short one.
 *
 *  The order is what makes the pick trustworthy. Rows arrive ranked by the
 *  gateway -- fit first, then the largest file inside that tier -- so the top
 *  of the list is the best thing that actually runs, and choosing it here is
 *  reading the fit gate rather than second-guessing it. Sorting in the browser
 *  would be a second answer to a question already answered, and the one that
 *  disagreed would be the one that misled.
 *
 *  Serve is absent on a row that cannot be served, rather than present and
 *  dead: a disabled button reads as "not right now" when the truth is "not by
 *  this route at all". */
export function QuantLadder({
  ladder,
  loading,
  error,
  context,
  concurrency,
}: {
  ladder: VariantLadder | null
  loading: boolean
  error: string | null
  context: number
  concurrency: number
}) {
  const { backend, invalidate } = useBackend()
  const [launching, setLaunching] = useState<string | null>(null)
  const [launchError, setLaunchError] = useState<string | null>(null)
  const [launched, setLaunched] = useState<string | null>(null)
  const [fittingOnly, setFittingOnly] = useState(false)
  const [openShards, setOpenShards] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)

  const serve = async (variant: QuantVariant) => {
    // The variant's own repository id, never the base model with a dtype
    // override: a quantization is a different repository, and the serve
    // command carries no --quantization to make an override mean anything.
    setLaunching(variant.repo_id)
    setLaunchError(null)
    try {
      await backend.launch({
        model_id: variant.repo_id,
        context,
        concurrency,
        target: 'throughput',
        runtime: 'vllm',
      })
      setLaunched(variant.repo_id)
      invalidate()
    } catch (e) {
      setLaunchError(e instanceof Error ? e.message : String(e))
    } finally {
      setLaunching(null)
    }
  }
  if (error) {
    return (
      <p
        className="label"
        style={{ fontWeight: 400, color: 'var(--fault)', whiteSpace: 'pre-wrap' }}
      >
        {error}
      </p>
    )
  }
  if (loading) {
    return <p className="unit">Looking for quantizations…</p>
  }
  if (!ladder || !ladder.variants.length) {
    return <p className="unit">No other quantization was found.</p>
  }

  // Rank, then label, so two rows the gateway ranked equally still draw in a
  // stable order across refetches.
  const ordered = [...ladder.variants].sort(
    (a, b) => a.rank - b.rank || a.label.localeCompare(b.label),
  )
  const hidden = fittingOnly ? ordered.filter((v) => v.fits !== true).length : 0
  const rows = fittingOnly ? ordered.filter((v) => v.fits === true) : ordered
  const recommended = ladder.recommended

  // The pick. `recommended` is the gateway's -- the largest variant that both
  // fits and can be served -- and when there is one it is the row to put the
  // button on. When there is not, the top of the ranking still gets shown
  // rather than nothing: "here is the closest thing, and here is why you
  // cannot run it" is an answer, and an empty card is not.
  const recommendedVariant =
    recommended != null
      ? (ordered.find(
          (v) => v.repo_id === recommended.repo_id && v.label === recommended.label,
        ) ?? null)
      : null
  const pick = recommendedVariant ?? ordered[0]!
  const pickLamp = fitLamp(pick)

  return (
    <>
      <p className="unit" style={{ margin: '0 0 8px' }}>
        Verdicts are from the fit gate at {context.toLocaleString()} context and{' '}
        {concurrency} {concurrency === 1 ? 'sequence' : 'sequences'}, against what the
        nodes can hand out right now.
      </p>

      {/* The pick, already made. Everything needed to act is on this one card:
          what it is, what it costs, whether it runs, and the button. */}
      <div className={recommendedVariant ? 'verdict on' : 'verdict'} style={{ marginBottom: 10 }}>
        <div className="vhead" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Lamp {...pickLamp} hollow={pickLamp.signal === 'idle'} />
          <span className="mono">{pick.label}</span>
          <span className="unit">{pick.dtype}</span>
          <span className="num">
            {pick.file_bytes != null ? `${gbytes(pick.file_bytes)} GiB` : '—'}
          </span>
          {pick.bits_per_weight != null ? (
            <span className="unit">{pick.bits_per_weight.toFixed(2)} bpw</span>
          ) : null}
          {pick.shard_count > 1 ? (
            <span className="unit">{pick.shard_count} shards</span>
          ) : null}
          <span style={{ marginLeft: 'auto' }}>
            {pick.launchable && pick.fits === true ? (
              launched === pick.repo_id ? (
                <span className="unit">launched</span>
              ) : (
                <button
                  className="ghost"
                  style={{ padding: '2px 10px', fontSize: 12 }}
                  disabled={launching === pick.repo_id}
                  onClick={() => void serve(pick)}
                >
                  {launching === pick.repo_id ? 'Serving…' : 'Serve'}
                </button>
              )
            ) : (
              // Absent, not disabled, on the pick card too.
              <span className="unit" title={pick.note}>
                not servable
              </span>
            )}
          </span>
        </div>
        <div className="unit" style={{ wordBreak: 'break-all', marginTop: 2 }}>
          {pick.repo_id}
          {pick.gguf_file ? ` · ${pick.gguf_file.split('/').pop()}` : ''}
        </div>
        {/* The fit gate's own sentence. Never paraphrased: it is more precise
            than a rewrite and it names the numbers it used. */}
        <Verbatim text={recommendedVariant ? recommended!.reason : pick.reason} size="label" />
        {recommendedVariant ? null : (
          <p className="unit" style={{ margin: '4px 0 0' }}>
            Nothing here both fits and can be served by a runtime on this cluster;
            this is the closest.
          </p>
        )}
      </div>

      {/* The list says its own size. A collapsed list that does not reads as a
          short one, and forty rows is the fact that made the pick worth
          making. */}
      {ordered.length > 1 ? (
        <button
          className="ghost"
          aria-expanded={showAll}
          style={{ padding: '2px 0', border: 0, fontSize: 12 }}
          onClick={() => setShowAll(!showAll)}
        >
          {showAll ? '– Hide the full list' : `+ Show all ${ordered.length} variants`}
        </button>
      ) : null}

      {!showAll ? null : (
      <>
      {/* Filters rows the gateway already judged. It decides nothing itself --
          `fits` is read, never recomputed. */}
      <label
        className="unit"
        style={{ display: 'flex', alignItems: 'center', gap: 6, margin: '8px 0' }}
      >
        <input
          type="checkbox"
          checked={fittingOnly}
          onChange={(e) => setFittingOnly(e.target.checked)}
        />
        Only show variants that fit
        {fittingOnly && hidden > 0 ? (
          // Never silently. A filtered list that does not say what it removed
          // reads as a complete one.
          <span className="unit">({hidden} hidden)</span>
        ) : null}
      </label>

      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Fit</th>
              <th>Variant</th>
              <th>Scheme</th>
              <th style={{ textAlign: 'right' }}>Download</th>
              <th style={{ textAlign: 'right' }}>bpw</th>
              <th>Repository</th>
              <th>File</th>
              <th>Serve</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((v) => {
              const key = `${v.repo_id}::${v.gguf_file ?? v.label}`
              return (
                <Row
                  key={key}
                  variant={v}
                  recommended={
                    recommended != null &&
                    v.repo_id === recommended.repo_id &&
                    v.label === recommended.label
                  }
                  launching={launching === v.repo_id}
                  launched={launched === v.repo_id}
                  shardsOpen={openShards === key}
                  onToggleShards={() => setOpenShards(openShards === key ? null : key)}
                  onServe={() => void serve(v)}
                />
              )
            })}
          </tbody>
        </table>
      </div>

      {rows.length === 0 ? (
        <p className="unit" style={{ marginTop: 8 }}>
          Nothing here fits at this context and concurrency.
        </p>
      ) : null}
      </>
      )}

      {/* Not a footnote anyone should be able to miss: the ladder is built
          from repository and file names, which is the only signal most
          quantizers leave. */}
      {launchError ? (
        <p
          className="label"
          style={{
            fontWeight: 400,
            color: 'var(--fault)',
            whiteSpace: 'pre-wrap',
            marginTop: 8,
          }}
        >
          {launchError}
        </p>
      ) : null}

      <p className="unit" style={{ marginTop: 8 }}>
        {ladder.note}
      </p>
    </>
  )
}

/** The fit verdict as one lamp.
 *
 *  Three tiers because the fit gate has three, and a fourth hollow state for
 *  the rows it could not judge -- an unchecked variant must not draw the same
 *  as a refused one. */
function fitLamp(variant: QuantVariant): { signal: 'live' | 'warn' | 'fault' | 'idle'; label: string } {
  switch (variant.verdict) {
    case 'fits':
      return { signal: 'live', label: 'fits' }
    case 'fits_degraded':
      return { signal: 'warn', label: 'loads, but decode is slow' }
    case 'wont_fit':
      return { signal: 'fault', label: 'will not fit' }
    default:
      return { signal: 'idle', label: 'not checked' }
  }
}

function Row({
  variant,
  recommended,
  launching,
  launched,
  shardsOpen,
  onToggleShards,
  onServe,
}: {
  variant: QuantVariant
  recommended: boolean
  launching: boolean
  launched: boolean
  shardsOpen: boolean
  onToggleShards: () => void
  onServe: () => void
}) {
  const fits = variant.fits
  const lamp = fitLamp(variant)
  const sharded = variant.shard_count > 1
  const file = variant.gguf_file
  const basename = file ? (file.split('/').pop() ?? file) : null

  return (
    <tr style={recommended ? { background: 'var(--panel-sunk)' } : undefined}>
      <td>
        <Lamp signal={lamp.signal} hollow={lamp.signal === 'idle'} label={lamp.label} />
      </td>
      <td className="mono" style={{ wordBreak: 'break-all' }}>
        {variant.label}
        {recommended ? <span className="pill" style={{ marginLeft: 6 }}>recommended</span> : null}
      </td>
      <td className="unit">{variant.dtype}</td>
      <td className="num">
        {/* Measured or absent. A size we did not measure is never drawn. */}
        {variant.file_bytes != null ? `${gbytes(variant.file_bytes)} GiB` : '—'}
      </td>
      <td className="num">
        {variant.bits_per_weight != null ? variant.bits_per_weight.toFixed(2) : '—'}
      </td>
      <td className="unit" style={{ wordBreak: 'break-all' }}>
        {variant.repo_id}
        {variant.downloads != null ? (
          <>
            <br />
            {variant.downloads.toLocaleString()} downloads
          </>
        ) : null}
      </td>
      <td className="unit" style={{ wordBreak: 'break-all', minWidth: 180 }}>
        {basename ?? '—'}
        {sharded ? (
          // The size in the row beside this one is the sum of these files.
          // Drawn as the list rather than a count so it is checkable.
          <Disclosure
            summary={`${variant.shard_count} shards, summed`}
            open={shardsOpen}
            onToggle={onToggleShards}
          >
            <div className="unit" style={{ wordBreak: 'break-all' }}>
              {variant.shard_files.map((f) => (
                <div key={f}>{f.split('/').pop() ?? f}</div>
              ))}
            </div>
          </Disclosure>
        ) : null}
      </td>
      <td>
        {!variant.launchable ? (
          // Absent, not disabled. A greyed button reads as "not right now";
          // the truth is that no runtime here loads this format at all, so the
          // reason is shown in place of the control.
          <span className="unit" title={variant.note}>
            not servable
          </span>
        ) : launched ? (
          <span className="unit">launched</span>
        ) : fits === true ? (
          <button
            className="ghost"
            style={{ padding: '2px 8px', fontSize: 12 }}
            disabled={launching}
            onClick={onServe}
          >
            {launching ? 'Serving…' : 'Serve'}
          </button>
        ) : (
          <span className="unit">{fits === false ? 'will not fit' : '—'}</span>
        )}
      </td>
    </tr>
  )
}
