import { useState } from 'react'
import type {
  Cluster,
  LadderBasis,
  Provider,
  PullAccepted,
  QuantVariant,
  VariantLadder,
} from '../../api/types'
import type { Runtime } from '../../state/runtime'
import { DEFAULT_CONCURRENCY, DEFAULT_CONTEXT } from '../../state/routes'
import { servesOnCluster } from '../../state/runtime'
import { alreadyOn } from './pullTargets'
import { ollamaRef, partitionForRuntime, variantKey } from './ollamaTarget'
import { ApiError } from '../../api/client'
import { useBackend } from '../../state/backend'
import { usePlacement } from '../../state/placement'
import { Disclosure } from '../../components/Panel'
import { Lamp } from '../../components/Lamp'
import { OverrideGate } from '../../components/OverrideGate'
import { Verbatim } from '../../components/Verbatim'
import { decodeLabel, fitLamp } from './ladder'
import { gbytes } from '../../format'
import type { CacheIndex } from './rows'

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
  target,
  runtime,
  provider,
  cache,
  cluster,
}: {
  ladder: VariantLadder | null
  loading: boolean
  error: string | null
  /** The OVERRIDE, or null for "the coordinator chose". Deliberately not what
   *  the caption is built from -- that comes off `ladder.sized_on` and each
   *  row's own numbers, so the pane always describes the answer that arrived
   *  rather than the question the client asked. Kept because Serve has to
   *  launch at whatever the verdict was taken at. */
  context: number | null
  concurrency: number | null
  /** The downloaded-weights join, and the cluster behind the post-launch chip.
   *  Both are polled once by ModelInspector and handed to the two halves of
   *  the pane. Mounting `useStorage` here as well would fan a second
   *  cluster-wide disk walk out every 30s for the same answer. */
  cache: CacheIndex
  cluster: Cluster | null
  /** Owned by ModelInspector, because the plan call above this list and the
   *  launch below it have to agree about them. Two copies would let the
   *  verdict describe one shape while Serve started another. */
  target: 'throughput' | 'latency'
  runtime: Runtime
  /** Where a pull would land, under a runtime that pulls. Null under vllm and
   *  sglang, and null when nothing is configured -- which is why Serve checks
   *  it rather than assuming the panel above supplied one. */
  provider: Provider | null
}) {
  const { backend, invalidate } = useBackend()
  const { nodeIds, degrees } = usePlacement()

  // Whether Serve on this list goes through the launcher or onto a provider.
  const onCluster = servesOnCluster(runtime)
  // Keyed on `variantKey`, never on repo_id. One GGUF repository publishes
  // many quantizations, so a repo-keyed state lights "Serving…" on every
  // sibling row of the one that was clicked.
  const [launching, setLaunching] = useState<string | null>(null)
  const [launchError, setLaunchError] = useState<string | null>(null)
  const [launched, setLaunched] = useState<string | null>(null)
  // What the pull endpoint accepted, when the runtime pulls. A pulled model
  // never becomes a Deployment, so the post-Serve chip has no cluster record
  // to resolve and reads this instead of waiting forever on one.
  const [pulled, setPulled] = useState<PullAccepted | null>(null)
  // A memory refusal, held with the variant it refused, and with the field
  // that would override it -- the cluster gate and the pull gate publish
  // different names. Cleared on every new attempt: an override must never
  // outlive the measurement that justified it.
  const [refusal, setRefusal] = useState<
    { key: string; message: string; param: string } | null
  >(null)
  const [override, setOverride] = useState(false)
  const [fittingOnly, setFittingOnly] = useState(false)
  const [openShards, setOpenShards] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [showReference, setShowReference] = useState(false)
  const serve = async (variant: QuantVariant, allowOverMemory = false) => {
    const key = variantKey(variant)
    setLaunching(key)
    setLaunchError(null)
    if (!allowOverMemory) setRefusal(null)
    try {
      if (onCluster) {
        // The variant's own repository id, never the base model with a dtype
        // override: a quantization is a different repository, and the serve
        // command carries no --quantization to make an override mean anything.
        await backend.launch({
          model_id: variant.repo_id,
          // What THIS row was judged at, not what the panel above was asked
          // for. On the default path nobody named a context and the gate chose
          // one per variant, so launching at the prop would start a deployment
          // the verdict beside the button was never taken at.
          context: variant.context ?? context ?? DEFAULT_CONTEXT,
          concurrency: variant.max_seqs ?? concurrency ?? DEFAULT_CONCURRENCY,
          target,
          runtime,
          // The machines and degrees chosen on the board above, so a launch
          // from this list lands where the panel above it says it will.
          // Spread, never sent as null: absence is the contract for "the
          // planner picks".
          //
          // With nothing ticked, fall back to the machines the VERDICT was
          // taken on rather than to "the planner picks". Same rule as
          // `context` above, and it became load-bearing when this ladder
          // stopped clamping itself to one machine: the row beside the button
          // is now judged at the widest degree the model admits across the
          // roster, and letting the planner re-choose would launch a placement
          // the verdict was never taken at. `sized_on` reports the PLACEMENT,
          // so a model that only splits one way still sends one machine
          // however many are enrolled. Empty only on a gateway that predates
          // the field, where the old behaviour is the right one.
          ...(nodeIds
            ? { node_ids: nodeIds }
            : sizedOn.nodes.length
              ? { node_ids: sizedOn.nodes }
              : {}),
          ...(degrees ? { parallelism: degrees } : {}),
          // Sent only after someone has read the sentence naming the measured
          // figure and ticked the box.
          ...(allowOverMemory ? { allow_over_live_memory: true } : {}),
        })
        setPulled(null)
      } else {
        // A different verb. Nothing is planned, nothing is launched, and no
        // Deployment appears -- the provider fetches the weights onto itself
        // and the gateway routes to it once its catalogue refreshes.
        const ref = ollamaRef(variant)
        if (!provider || !ref) {
          setLaunchError(
            provider
              ? 'That row has no GGUF file, so there is nothing for a provider to fetch.'
              : 'No provider is configured to pull onto. Add one under Settings → Providers.',
          )
          return
        }
        const reply = await backend.pullToProvider(provider.provider_id, {
          model: ref,
          ...(allowOverMemory ? { allow_over_memory: true } : {}),
        })
        setPulled(reply)
      }
      setLaunched(key)
      setRefusal(null)
      setOverride(false)
      invalidate()
    } catch (e) {
      // 409 is not 400. The request was legal and an unchanged retry succeeds
      // once memory frees, so it gets the override path rather than the error
      // line -- and a static refusal, which is a 400, correctly does not.
      const code = e instanceof ApiError && e.status === 409 ? refusalCode(e) : null
      if (code) {
        setRefusal({ key, message: e instanceof Error ? e.message : String(e), param: code })
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
  // What the rows were sized against, from the answer rather than from the
  // question. Defaulted for a gateway that predates the field, so an older
  // coordinator degrades to today's wording instead of rendering "undefined".
  const sizedOn: LadderBasis = ladder.sized_on ?? {
    nodes: [],
    probed_node: null,
    tensor_parallel: 1,
    budget_basis: 'gpu',
    local_serving: true,
  }

  const ordered = [...ladder.variants].sort(
    (a, b) => a.rank - b.rank || a.label.localeCompare(b.label),
  )
  const { servable, reference } = partitionForRuntime(ordered, runtime)

  // The fit filter only means something where there is a fit. Under a provider
  // runtime every row is unjudged, so filtering on `fits` would hide the whole
  // list on the strength of a verdict about another machine.
  const filtering = onCluster && fittingOnly
  const hidden = filtering ? servable.filter((v) => v.fits !== true).length : 0
  const rows = filtering ? servable.filter((v) => v.fits === true) : servable
  // The gateway's recommendation is for a runtime on this cluster: `_recommend`
  // requires `fits and launchable`, so it can only ever name a row this
  // runtime cannot fetch.
  const recommended = onCluster ? ladder.recommended : null

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
  const pickLamp = fitLamp(pick, onCluster)
  const pickKey = variantKey(pick)
  const refused = refusal
    ? (ordered.find((v) => variantKey(v) === refusal.key) ?? null)
    : null
  // Servability under this runtime, which is what the button is really asking.
  // Under the cluster runtimes that is the gateway's `launchable` and its fit
  // verdict together; under a provider runtime it is whether the row is a GGUF
  // and there is somewhere to send it -- the fit verdict describes a machine
  // that is not involved.
  const canServe = (v: QuantVariant) =>
    onCluster
      ? // `local_serving` is false when the verdicts were budgeted against
        // host memory on a machine with no GPU. Those figures are real and
        // worth showing -- they are what says whether the box could hold the
        // file at all -- but nothing here can be started on it, and a verdict
        // you cannot act on must not grow a button.
        sizedOn.local_serving !== false && v.launchable && v.fits === true
      : Boolean(provider) && Boolean(ollamaRef(v))

  return (
    <>
      <p className="unit" style={{ margin: '0 0 8px' }}>
        {!onCluster ? (
          <>
            Sizes are the measured download. Nothing here is judged by the fit gate:
            these weights would run on {provider ? provider.display_name : 'a provider'},
            whose memory derate does not manage — the pull is what weighs them, against
            that machine's own free memory.
          </>
        ) : (
          <>
            Verdicts are from the fit gate on {sizedOnPhrase(sizedOn)},{' '}
            {ladderContext(ladder)}
            {sizedOn.budget_basis === 'host_memory'
              ? ', against host memory — no GPU was found there, so nothing below can be served from it'
              : sizedOn.budget_is_live === false
                ? ', against the memory that hardware could spend with nothing else running — no node reported a live figure'
                : ', against what those machines can hand out right now'}
            .
          </>
        )}
        {/* What used to be here was an apology.

            `GET /api/models/variants` sized every row on ONE machine while the
            board above allowed several to be ticked, so the sentence said so:
            "Each row is sized on a single machine, so it does not account for
            the N you ticked above." That was honest and useless -- the numbers
            were still the wrong ones, the launch was still gated on different
            figures, and it only appeared when the operator had ticked the
            machines rather than when the planner chose them.

            The request now carries the machines, so the answer is about them,
            and the caption above states which ones and at what degree instead
            of hedging about what it does not know. Nothing to apologise for. */}
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
          {onCluster ? (
            <OnDisk repoId={pick.repo_id} expectedBytes={pick.file_bytes} cache={cache} />
          ) : (
            // A different disk. The cache index walks this cluster's nodes,
            // and the provider's storage is not among them -- so the question
            // becomes whether that box already lists this ref.
            <AlreadyThere provider={provider} variant={pick} />
          )}
          <span style={{ marginLeft: 'auto' }}>
            {canServe(pick) ? (
              launched === pickKey ? (
                <Served
                  repoId={pick.repo_id}
                  cluster={cluster}
                  pulled={onCluster ? null : pulled}
                />
              ) : (
                <button
                  className="ghost"
                  style={{ padding: '2px 10px', fontSize: 12 }}
                  disabled={launching === pickKey}
                  onClick={() => void serve(pick)}
                >
                  {launching === pickKey ? (onCluster ? 'Serving…' : 'Pulling…') : 'Serve'}
                </button>
              )
            ) : (
              // Absent, not disabled, on the pick card too.
              <span className="unit" title={pick.note}>
                {onCluster || provider ? 'not servable' : 'no provider configured'}
              </span>
            )}
          </span>
        </div>
        <div className="unit" style={{ wordBreak: 'break-all', marginTop: 2 }}>
          {pick.repo_id}
          {pick.gguf_file ? ` · ${pick.gguf_file.split('/').pop()}` : ''}
        </div>
        {/* The fit gate's own sentence. Never paraphrased: it is more precise
            than a rewrite and it names the numbers it used -- which is exactly
            why it is withheld under a runtime that serves from another
            machine. Rendering it there would put a sentence naming this
            cluster's memory beside a button that starts something somewhere
            else, and the promise the verbatim rule makes is that the numbers
            in the sentence are the numbers the gate used. */}
        {onCluster ? (
          <>
            <Verbatim
              text={recommendedVariant ? recommended!.reason : pick.reason}
              size="label"
            />
            <Consequence variant={pick} />
          </>
        ) : null}
        {/* Not on disk yet, so the first launch pulls it. Stated from the
            measured file size rather than a transfer nobody is watching: the
            control plane does not download weights, the runtime container
            does. */}
        {onCluster &&
        pick.launchable &&
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
            {!onCluster
              ? servable.length
                ? // Not "the gateway recommended nothing": it recommends for a
                  // runtime on this cluster, so it was never asked this
                  // question. Saying it declined would be putting words in it.
                  'The gateway ranks these for this cluster’s runtimes, not for a provider; this is its best-ranked GGUF.'
                : 'No GGUF was found in this repository, and Ollama loads nothing else.'
              : servable.length
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
              refusal!.param === 'allow_over_memory'
                ? 'Pull anyway. I am overriding the memory gate, which measured what ' +
                  'that machine has free and refused; the download will proceed and may ' +
                  'fill its disk.'
                : 'Serve anyway. I am overriding the live fit gate, which measured what ' +
                  'the node can hand out right now and refused; it would fit on an idle machine.'
            }
            checked={override}
            onChange={setOverride}
            onLaunch={() => void serve(refused, true)}
            launching={launching === variantKey(refused)}
          />
        </div>
      ) : null}

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
            : onCluster
              ? `+ Show all ${servable.length} that can be served here`
              : `+ Show all ${servable.length} a provider could fetch`}
        </button>
      ) : null}

      {!showAll ? null : (
        <>
          {/* Filters rows the gateway already judged. It decides nothing itself
              -- `fits` is read, never recomputed. Absent under a provider
              runtime: there is no verdict on these rows to filter on, and a
              checkbox that hid everything would look like an answer. */}
          {onCluster ? (
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
          ) : null}

          <div style={{ overflowX: 'auto' }}>
            <table>
              <thead>
                <tr>
                  <th>Fit</th>
                  <th>Variant</th>
                  <th>Scheme</th>
                  <th style={{ textAlign: 'right' }}>Download</th>
                  {/* Both are the fit gate's arithmetic about a GPU node in
                      this cluster. Under a provider runtime the columns are
                      dropped rather than blanked: a dash reads as "not
                      measured yet", and predicted decode from a node's memory
                      bandwidth would be wrong by orders of magnitude on a CPU
                      box, printed as a bare figure with nothing qualifying it. */}
                  {onCluster ? <th style={{ textAlign: 'right' }}>Headroom</th> : null}
                  {onCluster ? <th style={{ textAlign: 'right' }}>Decode</th> : null}
                  <th>Repository</th>
                  <th>File</th>
                  <th>Serve</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((v) => {
                  const key = variantKey(v)
                  return (
                    <Row
                      key={key}
                      variant={v}
                      cache={cache}
                      cluster={cluster}
                      onCluster={onCluster}
                      provider={provider}
                      servable={canServe(v)}
                      pulled={onCluster ? null : pulled}
                      recommended={
                        recommended != null &&
                        v.repo_id === recommended.repo_id &&
                        v.label === recommended.label
                      }
                      launching={launching === key}
                      launched={launched === key}
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
              {onCluster
                ? 'Nothing here fits at this context and concurrency.'
                : 'No GGUF was found in this repository, and Ollama loads nothing else.'}
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
            summary={
              onCluster
                ? `${reference.length} more that cannot be served here`
                : `${reference.length} more a provider cannot fetch`
            }
            open={showReference}
            onToggle={() => setShowReference(!showReference)}
          >
            <p className="unit" style={{ margin: '0 0 8px' }}>
              {/* The group's own note only describes it under the cluster
                  runtimes, where the reference rows are the GGUFs and the note
                  is about GGUF repositories. Under a provider runtime the
                  membership inverts -- these are the safetensors rows -- and
                  that note would read "one file inside a GGUF repository"
                  above a list containing none. */}
              {onCluster
                ? reference[0]!.note || 'No runtime on this cluster loads these formats.'
                : 'Ollama loads GGUF and nothing else, so these are not targets for a pull however well they fit here.'}
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
                    const lamp = fitLamp(v, onCluster)
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
function Served({
  repoId,
  cluster,
  pulled,
}: {
  repoId: string
  cluster: Cluster | null
  pulled: PullAccepted | null
}) {
  // A pull never becomes a Deployment. Looking for one would leave this
  // reading "starting…" forever, with no timeout and nothing to explain it --
  // so the accepted reply is the record here, and it is the only thing that
  // will ever arrive.
  if (pulled) return <Pulled reply={pulled} />
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
  cache: CacheIndex
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
  // Same guard as the headroom bit above it. A predicted throughput describes
  // a model that loaded; printed under a refusal it reads as a promise about
  // something that is not going to run.
  if (variant.predicted_decode_tps != null && variant.fits === true) {
    bits.push(`${variant.predicted_decode_tps.toFixed(0)} tok/s predicted decode`)
  }
  // The hardware holds it and something resident is in the way. Both sentences
  // belong on screen, each as the gate wrote it: the live one above says what
  // is free now, this one says what the machine could do idle. Neither
  // paraphrases the other, which is the only honest way to draw the
  // distinction -- and it is what makes the override gate's "it would fit on
  // an idle machine" checkable rather than merely asserted.
  const blockedNow = variant.fits === false && variant.static_fits === true
  if (!bits.length && !blockedNow) return null
  return (
    <>
      {bits.length ? (
        <p className="unit" style={{ margin: '4px 0 0' }}>
          {bits.join(' · ')}
        </p>
      ) : null}
      {blockedNow ? (
        <>
          <p className="unit" style={{ margin: '4px 0 0' }}>
            This hardware holds it. What is resident right now is what blocks it:
          </p>
          <Verbatim text={variant.static_reason ?? ''} size="unit" />
        </>
      ) : null}
    </>
  )
}

/** The override field for a 409, or null if this 409 is not an overridable one.
 *
 *  Two gates answer 409 on this screen and they publish different names. The
 *  cluster launch refuses with `live_memory_insufficient` and reopens on
 *  `allow_over_live_memory`; the pull refuses with `pull_over_memory` and
 *  reopens on `allow_over_memory`. Matching only the first left a real pull
 *  refusal rendering as a bare red line with the way past it unreachable.
 *
 *  Unrecognised codes return null deliberately: a 409 this screen has no
 *  override for is an error, and offering a checkbox that sends a field the
 *  gateway ignores would be a button that does nothing twice.
 */
function refusalCode(e: ApiError): string | null {
  try {
    const parsed = JSON.parse(e.body) as { error?: { code?: unknown } }
    switch (parsed.error?.code) {
      case 'live_memory_insufficient':
        return 'allow_over_live_memory'
      case 'pull_over_memory':
        return 'allow_over_memory'
      default:
        return null
    }
  } catch {
    return null
  }
}

/** The context clause. Every row carries its own, because on the default path
 *  the gate chose one -- and for a ladder they are all the same model, so one
 *  number describes the list whenever the rows agree. When they do not, say so
 *  rather than picking one and printing it over rows it is not about. */
function ladderContext(ladder: VariantLadder): string {
  const judged = ladder.variants.filter((v) => v.context != null)
  if (!judged.length) return 'at the context it chose'
  const contexts = new Set(judged.map((v) => v.context))
  const seqs = judged[0]!.max_seqs ?? 1
  const plural = seqs === 1 ? 'sequence' : 'sequences'
  if (contexts.size > 1) return `each at its own context, ${seqs} ${plural}`
  const only = judged[0]!.context!
  return `at ${only.toLocaleString()} context and ${seqs} ${plural}`
}

/** The machines a ladder was sized on, named.
 *
 *  Named and not counted, because "a single machine" was the old apology's
 *  phrasing and being unable to say WHICH was the whole complaint. The degree
 *  is stated whenever it is above 1: two machines sharded and two machines
 *  considered one at a time are different answers over the same node list. */
function sizedOnPhrase(basis: LadderBasis): string {
  const nodes = basis.nodes ?? []
  if (!nodes.length) return 'this cluster'
  const listed =
    nodes.length === 1
      ? nodes[0]!
      : `${nodes.slice(0, -1).join(', ')} and ${nodes[nodes.length - 1]}`
  return basis.tensor_parallel > 1
    ? `${listed}, tensor-parallel ${basis.tensor_parallel}`
    : listed
}


function Row({
  variant,
  cache,
  cluster,
  onCluster,
  provider,
  servable,
  pulled,
  recommended,
  launching,
  launched,
  shardsOpen,
  onToggleShards,
  onServe,
}: {
  variant: QuantVariant
  cache: CacheIndex
  cluster: Cluster | null
  onCluster: boolean
  provider: Provider | null
  servable: boolean
  pulled: PullAccepted | null
  recommended: boolean
  launching: boolean
  launched: boolean
  shardsOpen: boolean
  onToggleShards: () => void
  onServe: () => void
}) {
  const fits = variant.fits
  const lamp = fitLamp(variant, onCluster)
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
        {onCluster ? (
          <OnDisk repoId={variant.repo_id} expectedBytes={variant.file_bytes} cache={cache} />
        ) : (
          <AlreadyThere provider={provider} variant={variant} />
        )}
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
      {onCluster ? (
        <td className="num">
          {/* Only where it means something: headroom under a refusal is the size
              of the shortfall, which the reason already states in words. */}
          {variant.headroom != null && fits === true ? `${gbytes(variant.headroom)} GiB` : '—'}
        </td>
      ) : null}
      {onCluster ? (
        <td className="num">
          {/* Guarded like Headroom beside it, and for the same reason: a
              throughput is about a model that is resident. `fits` is true for
              `fits_degraded` too, so the row whose whole point IS its decode
              figure keeps it, and only the refusals and the unjudged lose it.
              This column used to print a number for every refusal -- computed,
              in the reported case, against a cache read for a 262,144-token
              window nothing had verified. */}
          {decodeLabel(variant)}
        </td>
      ) : null}
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
          <Served repoId={variant.repo_id} cluster={cluster} pulled={pulled} />
        ) : servable ? (
          <button
            className="ghost"
            style={{ padding: '2px 8px', fontSize: 12 }}
            disabled={launching}
            onClick={onServe}
          >
            {launching ? (onCluster ? 'Serving…' : 'Pulling…') : 'Serve'}
          </button>
        ) : (
          <span className="unit">
            {!onCluster ? 'no provider' : fits === false ? 'will not fit' : '—'}
          </span>
        )}
      </td>
    </tr>
  )
}

/** What the pull endpoint accepted, said in full.
 *
 *  Including whether a gate ran. The reply carries `free_bytes` and
 *  `budget_bytes`, and both are 0 in two entirely different situations: a
 *  machine that never joined the cluster and cannot be measured, and one that
 *  was measured and has nothing free. `gated` is what separates them, and
 *  without saying it the screen would report "0 GiB free" about a box nobody
 *  looked at -- a headroom figure invented out of an absent measurement, which
 *  is the one thing this project will not print.
 */
function Pulled({ reply }: { reply: PullAccepted }) {
  if (!reply.download_bytes) {
    return <span className="unit">already on {reply.checked_against}</span>
  }
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
      <Lamp signal="warn" label={`pulling ${reply.model}`} />
      <span className="unit">
        pulling {gbytes(reply.download_bytes, 2)} GiB
        {reply.gated
          ? ` — ${gbytes(reply.free_bytes, 2)} GiB free on ${reply.checked_against}`
          : ` — ${reply.checked_against} has not joined the cluster, so nothing weighed this`}
        . Progress is in the sidebar under Activity. Restarting the coordinator
        cancels it.
      </span>
      {/* The name it will answer to, which is NOT this repository's id.
          Ollama reports a pulled model under the ref it was given, so it
          arrives in /v1/models as `hf.co/<repo>:<tag>` and appears in the
          model list as its own row rather than merging into this one.
          Verified against Ollama 0.33.3, which echoes the tag case-preserved.

          Not aliased back deliberately: a provider alias is stored and
          displayed, so it goes through the full key screen -- the entropy
          heuristic included -- and that is exactly the check that refuses
          ordinary GGUF repository names. Fixing the display by reintroducing
          that failure for the names it most affects is a bad trade, so the
          divergence is stated instead of hidden. */}
      <span className="unit mono" style={{ wordBreak: 'break-all' }}>
        served as {reply.model}
      </span>
    </span>
  )
}

/** Whether the provider already lists this exact ref.
 *
 *  The counterpart to OnDisk, against a different disk: the cache index walks
 *  this cluster's nodes and a provider is not one of them. Decorative only --
 *  a case-sensitivity mismatch in how Ollama echoes an `hf.co/` ref back must
 *  cost a missing chip, never a button that will not press.
 */
function AlreadyThere({
  provider,
  variant,
}: {
  provider: Provider | null
  variant: QuantVariant
}) {
  if (!provider) return null
  const ref = ollamaRef(variant)
  if (!ref || !alreadyOn(provider, ref)) return null
  return <span className="pill">on {provider.display_name}</span>
}
