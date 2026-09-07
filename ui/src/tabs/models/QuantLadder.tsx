import { useMemo, useState } from 'react'
import type { Cluster, QuantVariant, VariantLadder } from '../../api/types'
import { ApiError } from '../../api/client'
import { useBackend } from '../../state/backend'
import { useCluster, useStorage } from '../../state/resources'
import { Disclosure } from '../../components/Panel'
import { Lamp } from '../../components/Lamp'
import { OverrideGate } from '../../components/OverrideGate'
import { Verbatim } from '../../components/Verbatim'
import { gbytes } from '../../format'
import { cacheIndex } from './rows'

/** One quantization, already chosen, with the rest a click away.
 *
 *  A repository can publish forty of these, and a table of forty rows asks a
 *  question most people cannot answer and should not have to. So this opens on
 *  a single pick -- the largest variant that both fits and can be served here
 *  -- with the button already beside it.
 *
 *  The rest are split in two, and the split is the point. Most of what a hub
 *  search turns up for a popular model is GGUF, and there is no llama.cpp
 *  runtime here, so those rows cannot be launched at all: for Qwen3-30B-A3B,
 *  thirty-seven of forty-one. The gateway ranks on fit before servability, so
 *  its ordering puts the four launchable rows at 29, 30, 31 and 39 -- a list
 *  that opens on twenty-nine dead ends. Partitioning fixes what someone sees
 *  without touching the ordering: rank order is preserved exactly inside each
 *  group, so this is still reading the gateway's answer rather than composing
 *  a second one. Re-sorting in the browser would be that second answer, and the
 *  one that disagreed would be the one that misled.
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
  const storage = useStorage()
  // Where a launch actually got to. `invalidate()` refreshes this, and the 5s
  // cluster poll keeps it moving afterwards.
  const cluster = useCluster()
  const [launching, setLaunching] = useState<string | null>(null)
  const [launchError, setLaunchError] = useState<string | null>(null)
  const [launched, setLaunched] = useState<string | null>(null)
  // A live-memory refusal, held with the variant it refused. Cleared on every
  // new attempt: an override must never outlive the measurement that justified
  // it, which is the same rule PlannerBar keeps on the dashboard.
  const [refusal, setRefusal] = useState<{ repo_id: string; message: string } | null>(null)
  const [override, setOverride] = useState(false)
  const [fittingOnly, setFittingOnly] = useState(false)
  const [openShards, setOpenShards] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [showReference, setShowReference] = useState(false)
  // Parity with the planner bar, which has exposed both since it shipped. The
  // ladder used to hardcode them, so the same Serve button meant two different
  // launches depending on which screen it was pressed from.
  const [target, setTarget] = useState<'throughput' | 'latency'>('throughput')
  const [runtime, setRuntime] = useState<'vllm' | 'sglang'>('vllm')

  const cache = useMemo(() => cacheIndex(storage.data), [storage.data])

  const serve = async (variant: QuantVariant, allowOverLiveMemory = false) => {
    // The variant's own repository id, never the base model with a dtype
    // override: a quantization is a different repository, and the serve
    // command carries no --quantization to make an override mean anything.
    setLaunching(variant.repo_id)
    setLaunchError(null)
    if (!allowOverLiveMemory) setRefusal(null)
    try {
      await backend.launch({
        model_id: variant.repo_id,
        context,
        concurrency,
        target,
        runtime,
        // Sent only after someone has read the sentence naming the measured
        // figure and ticked the box.
        ...(allowOverLiveMemory ? { allow_over_live_memory: true } : {}),
      })
      setLaunched(variant.repo_id)
      setRefusal(null)
      setOverride(false)
      invalidate()
    } catch (e) {
      // 409 is not 400. The request was legal and an unchanged retry succeeds
      // once memory frees, so it gets the override path rather than the error
      // line -- and a static refusal, which is a 400, correctly does not.
      if (e instanceof ApiError && e.status === 409 && isLiveMemoryRefusal(e)) {
        setRefusal({ repo_id: variant.repo_id, message: e.message })
        setOverride(false)
      } else {
        setLaunchError(e instanceof Error ? e.message : String(e))
      }
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
  if (loading) return <Skeleton />
  if (!ladder || !ladder.variants.length) {
    return <p className="unit">No other quantization was found.</p>
  }

  // Rank, then label, so two rows the gateway ranked equally still draw in a
  // stable order across refetches. This is the gateway's ordering, preserved.
  const ordered = [...ladder.variants].sort(
    (a, b) => a.rank - b.rank || a.label.localeCompare(b.label),
  )
  const servable = ordered.filter((v) => v.launchable)
  const reference = ordered.filter((v) => !v.launchable)

  const hidden = fittingOnly ? servable.filter((v) => v.fits !== true).length : 0
  const rows = fittingOnly ? servable.filter((v) => v.fits === true) : servable
  const recommended = ladder.recommended

  // The pick. `recommended` is the gateway's -- the largest variant that both
  // fits and can be served -- and when there is one it is the row to put the
  // button on. When there is not, the best servable row is shown rather than
  // the top of a ranking whose first rows cannot be launched at all; failing
  // that, the top of the ranking, because "here is the closest thing, and here
  // is why you cannot run it" is an answer and an empty card is not.
  const recommendedVariant =
    recommended != null
      ? (ordered.find(
          (v) => v.repo_id === recommended.repo_id && v.label === recommended.label,
        ) ?? null)
      : null
  const pick = recommendedVariant ?? servable[0] ?? ordered[0]!
  const pickLamp = fitLamp(pick)
  const refused = refusal ? (ordered.find((v) => v.repo_id === refusal.repo_id) ?? null) : null

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
        <div className="vhead" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
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
          <OnDisk repoId={pick.repo_id} expectedBytes={pick.file_bytes} cache={cache} />
          <span style={{ marginLeft: 'auto' }}>
            {pick.launchable && pick.fits === true ? (
              launched === pick.repo_id ? (
                <Launched repoId={pick.repo_id} cluster={cluster.data} />
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
        <Consequence variant={pick} />
        {/* Not on disk yet, so the first launch pulls it. Stated from the
            measured file size rather than a transfer nobody is watching: the
            control plane does not download weights, the runtime container
            does. */}
        {pick.launchable &&
        pick.file_bytes != null &&
        cache.complete(pick.repo_id, pick.file_bytes) !== true ? (
          <p className="unit" style={{ margin: '4px 0 0' }}>
            {cache.nodes(pick.repo_id).length
              ? `Only part of this is cached — the first launch pulls the rest of ${gbytes(pick.file_bytes)} GiB.`
              : `Not cached on any node — the first launch pulls ${gbytes(pick.file_bytes)} GiB.`}
          </p>
        ) : null}
        {recommendedVariant ? null : (
          <p className="unit" style={{ margin: '4px 0 0' }}>
            {servable.length
              ? 'The gateway recommended nothing here; this is the best one that can be served.'
              : 'Nothing here both fits and can be served by a runtime on this cluster; this is the closest.'}
          </p>
        )}
      </div>

      {/* The live-memory refusal, wherever it came from. Serve exists on the
          pick card and on every fitting row of the table, so a gate that only
          rendered inside the card would leave a row-triggered 409 with nowhere
          to go -- the launch would refuse and the screen would say nothing. */}
      {refused ? (
        <div className="verdict on bad" style={{ marginBottom: 10 }}>
          <div className="vhead" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Lamp signal="fault" label="refused on live memory" />
            <span className="mono">{refused.label}</span>
            <span className="unit" style={{ wordBreak: 'break-all' }}>
              {refused.repo_id}
            </span>
          </div>
          <OverrideGate
            reason={refusal!.message}
            sentence={
              'Serve anyway. I am overriding the live fit gate, which measured what ' +
              'the node can hand out right now and refused; it would fit on an idle machine.'
            }
            checked={override}
            onChange={setOverride}
            onLaunch={() => void serve(refused, true)}
            launching={launching === refused.repo_id}
          />
        </div>
      ) : null}

      <div className="bararea" style={{ marginBottom: 10 }}>
        <div className="fld">
          <label htmlFor="ql-target">Optimise for</label>
          <select
            id="ql-target"
            value={target}
            onChange={(e) => setTarget(e.target.value as 'throughput' | 'latency')}
          >
            <option value="throughput">throughput</option>
            <option value="latency">latency</option>
          </select>
        </div>
        <div className="fld">
          <label htmlFor="ql-runtime">Runtime</label>
          <select
            id="ql-runtime"
            value={runtime}
            onChange={(e) => setRuntime(e.target.value as 'vllm' | 'sglang')}
          >
            <option value="vllm">vllm</option>
            <option value="sglang">sglang</option>
          </select>
        </div>
      </div>

      {/* The list says its own size. A collapsed list that does not reads as a
          short one, and forty rows is the fact that made the pick worth
          making. */}
      {servable.length > 1 ? (
        <button
          className="ghost"
          aria-expanded={showAll}
          style={{ padding: '2px 0', border: 0, fontSize: 12 }}
          onClick={() => setShowAll(!showAll)}
        >
          {showAll
            ? '– Hide the servable list'
            : `+ Show all ${servable.length} that can be served here`}
        </button>
      ) : null}

      {!showAll ? null : (
        <>
          {/* Filters rows the gateway already judged. It decides nothing itself
              -- `fits` is read, never recomputed. */}
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
              // Never silently. A filtered list that does not say what it
              // removed reads as a complete one.
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
                  <th style={{ textAlign: 'right' }}>Headroom</th>
                  <th style={{ textAlign: 'right' }}>Decode</th>
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
                      cache={cache}
                      cluster={cluster.data}
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

      {/* Everything no runtime here can load, in one collapsed group with the
          reason said once. Kept rather than filtered out: the variants exist,
          somebody may be looking for one, and a list that quietly drops most of
          a repository is lying about the repository. */}
      {reference.length ? (
        <div style={{ marginTop: 10 }}>
          <Disclosure
            summary={`${reference.length} more that cannot be served here`}
            open={showReference}
            onToggle={() => setShowReference(!showReference)}
          >
            <p className="unit" style={{ margin: '0 0 8px' }}>
              {reference[0]!.note ||
                'No runtime on this cluster loads these formats.'}
            </p>
            <div style={{ overflowX: 'auto' }}>
              <table>
                <thead>
                  <tr>
                    <th>Fit</th>
                    <th>Variant</th>
                    <th>Scheme</th>
                    <th style={{ textAlign: 'right' }}>Download</th>
                    <th>Repository</th>
                    <th>File</th>
                  </tr>
                </thead>
                <tbody>
                  {reference.map((v) => {
                    const lamp = fitLamp(v)
                    const basename = v.gguf_file?.split('/').pop() ?? null
                    return (
                      <tr key={`${v.repo_id}::${v.gguf_file ?? v.label}`}>
                        <td>
                          <Lamp
                            signal={lamp.signal}
                            hollow={lamp.signal === 'idle'}
                            label={lamp.label}
                          />
                        </td>
                        <td className="mono" style={{ wordBreak: 'break-all' }}>
                          {v.label}
                        </td>
                        <td className="unit">{v.dtype}</td>
                        <td className="num">
                          {v.file_bytes != null ? `${gbytes(v.file_bytes)} GiB` : '—'}
                        </td>
                        <td className="unit" style={{ wordBreak: 'break-all' }}>
                          {v.repo_id}
                        </td>
                        <td className="unit" style={{ wordBreak: 'break-all' }}>
                          {basename ?? '—'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </Disclosure>
        </div>
      ) : null}

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

      {/* Not a footnote anyone should be able to miss: the ladder is built
          from repository and file names, which is the only signal most
          quantizers leave. */}
      <p className="unit" style={{ marginTop: 8 }}>
        {ladder.note}
      </p>
    </>
  )
}

/** The variants call is a hub search plus a repository read per GGUF repo and
 *  takes seconds. Blanking the section to one line of text for that long reads
 *  as an empty answer rather than a pending one. */
function Skeleton() {
  return (
    <div aria-busy="true" aria-live="polite">
      <p className="unit" style={{ margin: '0 0 8px' }}>
        Looking for quantizations…
      </p>
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="deprow"
          style={{ gridTemplateColumns: '1fr 2fr auto', opacity: 0.38 }}
          aria-hidden
        >
          <span style={{ background: 'var(--panel-sunk)', height: 12, borderRadius: 2 }} />
          <span style={{ background: 'var(--panel-sunk)', height: 12, borderRadius: 2 }} />
          <span style={{ background: 'var(--panel-sunk)', height: 12, width: 48, borderRadius: 2 }} />
        </div>
      ))}
    </div>
  )
}

/** Where the launch got to.
 *
 *  "launched" on its own was a dead end: the button vanished, one word replaced
 *  it, and nothing said whether the thing was starting, serving or already dead
 *  in a pull. The deployment record answers all three, and it is already being
 *  polled -- this only has to find the row and render its state.
 *
 *  Before the record exists there is a real gap of a second or two, which is
 *  said as "starting" rather than left blank. */
function Launched({ repoId, cluster }: { repoId: string; cluster: Cluster | null }) {
  const dep = (cluster?.deployments ?? []).find((d) => d.model_id === repoId)
  if (!dep) return <span className="unit">starting…</span>
  // `degraded` is warn, not fault: it is serving. `stopping` is warn for the
  // same reason -- it is still answering until it is not.
  const signal =
    dep.state === 'ready'
      ? 'live'
      : dep.state === 'failed' || dep.state === 'stopped'
        ? 'fault'
        : 'warn'
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <Lamp signal={signal} label={`${dep.served_name}: ${dep.state}`} />
      <span className="unit">{dep.served_name}</span>
      <span className="unit">{dep.state}</span>
      {dep.last_error ? (
        <span className="unit" style={{ color: 'var(--fault)' }} title={dep.last_error}>
          — {dep.last_error}
        </span>
      ) : null}
    </span>
  )
}

/** Already on a node's disk, so the first launch does not have to pull it.
 *  A badge rather than a second lamp: the one lamp on a row means fit.
 *
 *  Presence is not completeness, and here -- unlike the model list -- there is
 *  a measured size to check against, so the distinction can be drawn properly.
 *  A repository whose cache holds only `config.json` is a real case on this
 *  cluster (`Qwen/Qwen3-30B-A3B` is 2 MB of metadata and no weights), and a
 *  badge that called that "on disk" would promise a download that has not
 *  happened. */
function OnDisk({
  repoId,
  expectedBytes,
  cache,
}: {
  repoId: string
  expectedBytes?: number | null
  cache: ReturnType<typeof cacheIndex>
}) {
  const nodes = cache.nodes(repoId)
  if (!nodes.length) return null
  const where = nodes.length === 1 ? nodes[0] : `${nodes.length} nodes`
  const complete = cache.complete(repoId, expectedBytes)
  const have = cache.bytes(repoId)
  return (
    <span
      className="pill"
      style={complete === false ? { color: 'var(--warn)' } : undefined}
      title={
        complete === false && have != null && expectedBytes != null
          ? `${gbytes(have)} GiB of ${gbytes(expectedBytes)} GiB present on ${nodes.join(', ')}`
          : nodes.join(', ')
      }
    >
      {complete === false ? `partly on ${where}` : `on ${where}`}
    </span>
  )
}

/** What serving it would cost and buy, from figures already on the wire and
 *  never rendered until now. */
function Consequence({ variant }: { variant: QuantVariant }) {
  const bits: string[] = []
  if (variant.headroom != null && variant.fits === true) {
    bits.push(`${gbytes(variant.headroom)} GiB spare`)
  }
  if (variant.predicted_decode_tps != null) {
    bits.push(`${variant.predicted_decode_tps.toFixed(0)} tok/s predicted decode`)
  }
  if (!bits.length) return null
  return (
    <p className="unit" style={{ margin: '4px 0 0' }}>
      {bits.join(' · ')}
    </p>
  )
}

/** A 409 the launch path defines: the request is legal, the live budget is
 *  what refused it, and an unchanged retry succeeds once memory frees. */
function isLiveMemoryRefusal(e: ApiError): boolean {
  try {
    const parsed = JSON.parse(e.body) as { error?: { code?: unknown } }
    return parsed.error?.code === 'live_memory_insufficient'
  } catch {
    return false
  }
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
  cache,
  cluster,
  recommended,
  launching,
  launched,
  shardsOpen,
  onToggleShards,
  onServe,
}: {
  variant: QuantVariant
  cache: ReturnType<typeof cacheIndex>
  cluster: Cluster | null
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
        <OnDisk repoId={variant.repo_id} expectedBytes={variant.file_bytes} cache={cache} />
      </td>
      <td className="unit">
        {variant.dtype}
        {variant.bits_per_weight != null ? (
          <>
            <br />
            {variant.bits_per_weight.toFixed(2)} bpw
          </>
        ) : null}
      </td>
      <td className="num">
        {/* Measured or absent. A size we did not measure is never drawn. */}
        {variant.file_bytes != null ? `${gbytes(variant.file_bytes)} GiB` : '—'}
      </td>
      <td className="num">
        {/* Only where it means something: headroom under a refusal is the size
            of the shortfall, which the reason already states in words. */}
        {variant.headroom != null && fits === true ? `${gbytes(variant.headroom)} GiB` : '—'}
      </td>
      <td className="num">
        {variant.predicted_decode_tps != null
          ? `${variant.predicted_decode_tps.toFixed(0)} tok/s`
          : '—'}
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
        {launched ? (
          <Launched repoId={variant.repo_id} cluster={cluster} />
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
