import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ReactFlowProvider, useReactFlow } from '@xyflow/react'
import { ApiError } from '../api/client'
import type { MtpCapability, PlanRequest, ReachReport, WiringOp, WiringResult } from '../api/types'
import { IssueList, type IssueRow } from '../components/IssueList'
import { useBackend, useKeyedResource, useResource } from '../state/backend'
import { nameIndex } from '../state/names'
import { useCluster, useProviderCatalogues, useProviders, useRouting, useTopology } from '../state/resources'
import { useSelection } from '../state/selection'
import type { EditorApi } from './cluster/blocks/editor'
import {
  addLimit,
  addModel,
  addProviderModel,
  blocks as blocksOf,
  connect,
  dealPositions,
  dropFlag,
  diff,
  disconnect,
  fromServer,
  launches as launchesOf,
  relaunchesOf,
  removalSentence,
  removeServedName,
  limitsSupported,
  planRequest,
  rebase,
  removeBlock,
  removeProviderModel,
  setLimit,
  setModel,
  setProviderEnabled,
  setProviderPin,
  stops as stopsOf,
  singlePlanKey,
  takeServer,
  validate,
  wires as wiresOf,
  type Doc,
  type Issue,
  type PlanState,
  type ServerSnapshot,
  type Size,
  type XY,
} from './cluster/doc'
import { BalancerDialog } from './cluster/BalancerDialog'
import { GraphToolbar } from './cluster/GraphToolbar'
import { readPositions, writePositions } from './cluster/positions'
import { SelectionRail } from './cluster/SelectionRail'
import { LEGEND, SOCKET_TYPES, SOCKET_TYPE_ORDER, wireId, type SocketRef, type SocketSpec } from './cluster/sockets'
import { WiringCanvas } from './cluster/WiringCanvas'

const PLAN_DEBOUNCE_MS = 450
const DRY_RUN_DEBOUNCE_MS = 500
const DETAIL_DEBOUNCE_MS = 600
const SPEC_POLL_MS = 60000

interface SaveRow {
  key: string
  label: string
  status: 'done' | 'failed' | 'not applied' | 'launch requested'
  message: string | null
}

/** The block a dry-run row belongs on. */
function blockForOp(op: WiringOp | undefined): string | undefined {
  if (!op) return undefined
  return op.op === 'provider.patch' ? `provider:${op.provider_id}` : `split:${op.served_name}`
}

/** The two size bands the server files NCCL records under. Structural rather
 *  than fetched: the decode all-reduce a TP step issues is
 *  `hidden_size x 2 x batch` and the prefill one is megabytes. */
const DECODE_BAND = 8192
const BULK_BAND = 4194304

function opLabel(op: WiringOp): string {
  return op.op === 'provider.patch' ? `${op.op} ${op.provider_id}` : `${op.op} ${op.served_name}`
}

/** A 400 `wiring_invalid` carries its per-op results in the body. */
function refusedResults(err: unknown): WiringResult[] | null {
  if (!(err instanceof ApiError) || err.status !== 400) return null
  try {
    const parsed: unknown = JSON.parse(err.body)
    if (parsed && typeof parsed === 'object' && 'results' in parsed && Array.isArray(parsed.results)) {
      return parsed.results as WiringResult[]
    }
  } catch {
    // Not JSON: the caller renders err.message as one failed row.
  }
  return null
}

function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

/** The Cluster destination: the wiring editor and the reference cards under it. */
export function ClusterTab({ active }: { active: boolean }) {
  return (
    <ReactFlowProvider>
      <Editor active={active} />
    </ReactFlowProvider>
  )
}

function Editor({ active }: { active: boolean }) {
  const cluster = useCluster()
  const topology = useTopology()
  const routing = useRouting()
  const providers = useProviders()
  const { backend, invalidate } = useBackend()
  const { openSheet } = useSelection()
  const flow = useReactFlow()
  const providerIds = useMemo(() => (providers.data ?? []).map((p) => p.provider_id), [providers.data])
  const catalogues = useProviderCatalogues(providerIds)
  const registry = useResource((b) => b.modelRegistry(), 300000)
  const spec = useKeyedResource('wiring-spec', (b) => b.usage({ start: '-15m', bucket: '1h', group_by: ['model'] }), SPEC_POLL_MS)
  const [mtp, setMtp] = useState<Record<string, MtpCapability | null>>({})

  // ── The server's state, and the draft over it ─────────────────────────────

  const specRates = useMemo(() => {
    const sums = new Map<string, { accepted: number; drafted: number }>()
    for (const row of spec.data?.rows ?? []) {
      const name = row.group.model
      if (typeof name !== 'string' || row.spec_draft_tokens == null) continue
      const acc = sums.get(name) ?? { accepted: 0, drafted: 0 }
      acc.accepted += row.spec_accepted_tokens ?? 0
      acc.drafted += row.spec_draft_tokens
      sums.set(name, acc)
    }
    return Object.fromEntries([...sums].map(([k, v]) => [k, v.drafted > 0 ? v.accepted / v.drafted : null]))
  }, [spec.data])

  const snapshot = useMemo<ServerSnapshot | null>(() => {
    if (!topology.data || !cluster.data || !routing.data || !providers.data) return null
    return {
      topology: topology.data,
      cluster: cluster.data,
      routing: routing.data,
      providers: providers.data,
      catalogues: catalogues.data?.byProvider ?? {},
      mtp,
      spec: specRates,
    }
  }, [topology.data, cluster.data, routing.data, providers.data, catalogues.data, mtp, specRates])

  const [base, setBase] = useState<Doc | null>(null)
  const [doc, setDoc] = useState<Doc | null>(null)
  const [conflicts, setConflicts] = useState<string[]>([])
  const docRef = useRef(doc)
  docRef.current = doc
  const baseRef = useRef(base)
  baseRef.current = base

  // Every poll folds into the draft: untouched entities follow the server,
  // edited ones stay, and a both-sides change is kept as a conflict until
  // the operator takes the server's or the two agree again.
  const serverDoc = useMemo(() => (snapshot ? fromServer(snapshot) : null), [snapshot])
  const serverKey = serverDoc ? JSON.stringify(serverDoc) : ''
  useEffect(() => {
    if (!serverDoc || !snapshot) return
    const prevBase = baseRef.current
    const draft = docRef.current
    if (!prevBase || !draft) {
      setBase(serverDoc)
      setDoc(serverDoc)
      return
    }
    const r = rebase(prevBase, draft, serverDoc, snapshot.cluster.deployments)
    setDoc(r.doc)
    setBase(serverDoc)
    setConflicts((prev) => {
      const still = prev.filter((key) => {
        const name = key.slice(key.indexOf(':') + 1)
        return key.startsWith('provider:')
          ? !sameJson(r.doc.providers[name], serverDoc.providers[name])
          : !sameJson(r.doc.routes[name], serverDoc.routes[name])
      })
      return [...new Set([...still, ...r.conflicts])].sort()
    })
    // serverKey stands for serverDoc: a poll that changed nothing does nothing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey])

  // ── Layout ─────────────────────────────────────────────────────────────────

  const clusterId = topology.data?.cluster_id ?? ''
  const [stored, setStored] = useState<Record<string, XY>>({})
  useEffect(() => setStored(clusterId ? readPositions(clusterId) : {}), [clusterId])
  const moved = useCallback(
    (id: string, xy: XY) => {
      setStored((prev) => {
        const next = { ...prev, [id]: xy }
        if (clusterId) writePositions(clusterId, next)
        return next
      })
    },
    [clusterId],
  )
  // What React Flow measured per block: the deal stacks by real heights, so
  // nothing is placed on top of anything once the canvas has drawn it.
  const [sizes, setSizes] = useState<Record<string, Size>>({})
  const measured = useCallback((m: Record<string, Size>) => {
    setSizes((prev) => {
      let changed = false
      const next = { ...prev }
      for (const [id, s] of Object.entries(m)) {
        const w = Math.round(s.w)
        const h = Math.round(s.h)
        if (prev[id]?.w === w && prev[id]?.h === h) continue
        next[id] = { w, h }
        changed = true
      }
      return changed ? next : prev
    })
  }, [])
  const arrange = useCallback(() => {
    setStored({})
    if (clusterId) writePositions(clusterId, {})
    setLive('Blocks arranged: columns left to right, nothing overlapping.')
  }, [clusterId])

  // ── Derived drawing ────────────────────────────────────────────────────────

  const blocks = useMemo(() => (doc && snapshot ? blocksOf(doc, snapshot) : []), [doc, snapshot])
  const wires = useMemo(() => (doc && snapshot ? wiresOf(doc, snapshot) : []), [doc, snapshot])
  const positions = useMemo(() => dealPositions(blocks, stored, sizes), [blocks, stored, sizes])
  const ops = useMemo(() => (base && doc && snapshot ? diff(base, doc, snapshot) : []), [base, doc, snapshot])
  const opsKey = JSON.stringify(ops)

  // ── Plans for draft models ─────────────────────────────────────────────────

  const [plans, setPlans] = useState<Record<string, PlanState>>({})
  const planTimers = useRef(new Map<string, number>())
  useEffect(() => {
    const wanted = new Map<string, PlanRequest>()
    for (const m of doc?.models ?? []) {
      if (!m.model_id || m.node_ids.length === 0) continue
      wanted.set(m.id, planRequest(m))
      if (m.node_ids.length > 1) wanted.set(singlePlanKey(m.id), planRequest(m, [m.node_ids[0]!]))
    }
    setPlans((prev) => Object.fromEntries(Object.entries(prev).filter(([id]) => wanted.has(id))))
    for (const [id, req] of wanted) {
      const key = JSON.stringify(req)
      if (plans[id]?.key === key) continue
      window.clearTimeout(planTimers.current.get(id))
      planTimers.current.set(
        id,
        window.setTimeout(() => {
          setPlans((p) => ({ ...p, [id]: { key, checking: true, result: p[id]?.result ?? null, error: null } }))
          backend
            .plan(req)
            .then((result) => setPlans((p) => (p[id]?.key === key ? { ...p, [id]: { key, checking: false, result, error: null } } : p)))
            .catch((e: unknown) =>
              setPlans((p) =>
                p[id]?.key === key
                  ? { ...p, [id]: { key, checking: false, result: null, error: e instanceof Error ? e.message : String(e) } }
                  : p,
              ),
            )
        }, PLAN_DEBOUNCE_MS),
      )
    }
    // plans is read for its keys only; re-running on every plan landing would
    // reschedule what just answered.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc?.models, backend])

  // ── MTP capability per draft model id ──────────────────────────────────────

  useEffect(() => {
    const ids = [...new Set((doc?.models ?? []).map((m) => m.model_id))].filter((id) => id.includes('/') && !(id in mtp))
    if (ids.length === 0) return
    const t = window.setTimeout(() => {
      for (const id of ids) {
        backend
          .modelDetail(id)
          .then((d) => setMtp((prev) => ({ ...prev, [id]: d.capabilities?.mtp ?? null })))
          .catch(() => setMtp((prev) => ({ ...prev, [id]: null })))
      }
    }, DETAIL_DEBOUNCE_MS)
    return () => window.clearTimeout(t)
  }, [doc?.models, backend, mtp])

  // ── Dry run ────────────────────────────────────────────────────────────────

  const [dry, setDry] = useState<{ key: string; results: WiringResult[] | null; error: string | null } | null>(null)
  useEffect(() => {
    if (ops.length === 0) {
      setDry(null)
      return
    }
    const key = opsKey
    const t = window.setTimeout(() => {
      backend
        .wiring({ dry_run: true, ops })
        .then((r) => setDry({ key, results: r.results, error: null }))
        .catch((e: unknown) => {
          const results = refusedResults(e)
          setDry({
            key,
            results,
            error: results ? null : e instanceof ApiError ? e.message : 'The coordinator did not answer the dry run.',
          })
        })
    }, DRY_RUN_DEBOUNCE_MS)
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opsKey, backend])

  // ── Issues ─────────────────────────────────────────────────────────────────

  const issues = useMemo<Issue[]>(() => {
    if (!doc || !snapshot) return []
    const out = validate(doc, snapshot, plans, conflicts)
    if (dry && dry.key === opsKey) {
      for (const r of dry.results ?? []) {
        if (r.ok || !r.error) continue
        out.push({ id: `dry:${r.index}`, level: 'error', message: r.error.message, block: blockForOp(ops[r.index]) })
      }
      if (dry.error) out.push({ id: 'dry:transport', level: 'warn', message: dry.error })
    }
    return out
  }, [doc, snapshot, plans, conflicts, dry, opsKey, ops])
  const blocking = issues.filter((i) => i.level === 'error' && !i.live).length

  // ── Edits ──────────────────────────────────────────────────────────────────

  const [live, setLive] = useState('')
  const edit = useCallback((fn: (d: Doc) => Doc) => setDoc((d) => (d ? fn(d) : d)), [])

  const specIndex = useMemo(() => {
    const m = new Map<string, SocketSpec>()
    for (const b of blocks) for (const s of b.sockets) m.set(`${s.block}\u0000${s.socket}`, s)
    return m
  }, [blocks])
  const wireIds = useMemo(() => new Set(wires.map((w) => w.id)), [wires])

  const editor = useMemo<EditorApi>(
    () => ({
      specOf: (block, socket) => specIndex.get(`${block}\u0000${socket}`),
      wired: (o, i) => wireIds.has(wireId(o, i)),
      announce: setLive,
      issuesFor: (block) => issues.filter((i) => i.block === block),
      plans,
      modelIds: (registry.data?.models ?? []).map((m) => m.model_id),
      setLimit: (id, patch) => edit((d) => setLimit(d, id, patch)),
      setModel: (id, patch) => edit((d) => setModel(d, id, patch)),
      removeBlock: (id) => edit((d) => removeBlock(d, id)),
      setProviderEnabled: (p, v) => edit((d) => setProviderEnabled(d, p, v)),
      addProviderModel: (p, u, served) => edit((d) => addProviderModel(d, p, u, served)),
      removeProviderModel: (p, u) => edit((d) => removeProviderModel(d, p, u)),
      catalogueFor: (p) =>
        (catalogues.data?.byProvider[p] ?? []).filter((m) => !(m.upstream_id in (doc?.providers[p]?.models ?? {}))),
      openDeployment: (served) => openSheet({ kind: 'dep', id: served }),
      cut: (id) => edit((d) => disconnect(d, id)),
      dropFlag: (id, args, flag) => edit((d) => dropFlag(d, id, args, flag)),
      setProviderPin: (p, u, pin) => edit((d) => setProviderPin(d, p, u, pin)),
    }),
    [specIndex, wireIds, issues, plans, registry.data, edit, catalogues.data, doc, openSheet],
  )

  const onConnect = useCallback((from: SocketRef, to: SocketRef) => edit((d) => connect(d, from, to)), [edit])
  const onCut = useCallback((id: string) => edit((d) => disconnect(d, id)), [edit])
  const onRemove = useCallback((id: string) => edit((d) => removeBlock(d, id)), [edit])

  // ── Load balancers: the dialog, and removal ───────────────────────────────

  const [dialog, setDialog] = useState<string | null>(null)
  const onRemoveServed = useCallback(
    (served: string) => {
      if (!doc || !snapshot) return
      if (!window.confirm(removalSentence(doc, snapshot, served))) return
      edit((d) => removeServedName(d, snapshot, served))
    },
    [doc, snapshot, edit],
  )

  // Draft launches; relaunches are their own list, so each row can say which
  // deployment it brings back and without what.
  const launches = useMemo(() => (doc ? launchesOf(doc, plans) : []), [doc, plans])
  const relaunches = useMemo(() => (doc && snapshot ? relaunchesOf(doc, snapshot) : []), [doc, snapshot])
  const stopIds = useMemo(() => (doc ? stopsOf(doc) : []), [doc])
  const removedCount = doc?.removals.deployments.length ?? 0
  const dirty = base != null && doc != null && !sameJson(base, doc)
  const deploymentOf = useCallback(
    (id: string) => snapshot?.cluster.deployments.find((d) => d.deployment_id === id) ?? null,
    [snapshot],
  )
  const stopLabel = (id: string) => {
    const d = deploymentOf(id)
    return d ? `stop ${d.model_id} on ${d.node_ids.join(', ')}` : `stop ${id}`
  }

  // A block from the toolbar takes the first free slot in its column and is
  // pinned there, so it never lands on one already drawn.
  const spawn = (id: string, fn: (d: Doc) => Doc) => {
    if (!doc || !snapshot) return
    const next = fn(doc)
    const xy = dealPositions(blocksOf(next, snapshot), stored, sizes)[id]
    edit(fn)
    if (xy) moved(id, xy)
  }
  const addDraftModel = () => {
    const id = `model:${crypto.randomUUID()}`
    spawn(id, (d) => addModel(d, id))
  }
  const addRateLimit = () => {
    const id = `limit:${crypto.randomUUID()}`
    spawn(id, (d) => addLimit(d, id, { rpm: 60 }))
  }

  // ── Save ───────────────────────────────────────────────────────────────────

  const [saving, setSaving] = useState(false)
  const [saveRows, setSaveRows] = useState<SaveRow[]>([])
  const save = async () => {
    setSaving(true)
    const rows: SaveRow[] = []
    let applied = true
    try {
      if (ops.length > 0) {
        try {
          const r = await backend.wiring({ ops })
          for (const res of r.results) rows.push(rowFor(ops, res))
          applied = r.results.every((res) => res.ok)
        } catch (e) {
          applied = false
          const results = refusedResults(e)
          if (results) for (const res of results) rows.push(rowFor(ops, res))
          else rows.push({ key: 'wiring', label: 'wiring', status: 'failed', message: e instanceof Error ? e.message : String(e) })
        }
      }
      // Stops and launches only after the wiring they may depend on is in
      // place; a relaunch only once its stop went through.
      const stopped = new Set<string>()
      for (const id of applied ? stopIds : []) {
        try {
          await backend.stopDeployment(id)
          stopped.add(id)
          rows.push({ key: `stop:${id}`, label: stopLabel(id), status: 'done', message: null })
        } catch (e) {
          rows.push({ key: `stop:${id}`, label: stopLabel(id), status: 'failed', message: e instanceof Error ? e.message : String(e) })
        }
      }
      for (const [k, req] of (applied ? launches : []).entries()) {
        const label = `launch ${req.model_id} on ${(req.node_ids ?? []).join(', ')}`
        try {
          await backend.launch(req)
          rows.push({ key: `launch:${k}`, label, status: 'launch requested', message: null })
        } catch (e) {
          rows.push({ key: `launch:${k}`, label, status: 'failed', message: e instanceof Error ? e.message : String(e) })
        }
      }
      for (const r of applied ? relaunches : []) {
        const label = `relaunch ${r.model_id} on ${r.node_ids.join(', ')} without ${r.without.join(', ')}`
        if (!stopped.has(r.deployment_id)) {
          rows.push({ key: `relaunch:${r.deployment_id}`, label, status: 'not applied', message: 'Not attempted: the stop failed.' })
          continue
        }
        try {
          await backend.launch(r.request)
          rows.push({ key: `relaunch:${r.deployment_id}`, label, status: 'launch requested', message: null })
        } catch (e) {
          rows.push({ key: `relaunch:${r.deployment_id}`, label, status: 'failed', message: e instanceof Error ? e.message : String(e) })
        }
      }
      // What landed is no longer pending: a stopped deployment's removal and
      // relaunch, and (once the batch applied) the route clears. A stop that
      // failed stays, to be tried again.
      if (applied) {
        edit((d) => ({
          ...d,
          relaunches: Object.fromEntries(Object.entries(d.relaunches).filter(([id]) => !stopped.has(id))),
          removals: { deployments: d.removals.deployments.filter((id) => !stopped.has(id)), routes: [] },
        }))
      }
    } finally {
      setSaveRows(rows)
      setSaving(false)
      invalidate()
    }
  }

  const discard = () => {
    if (base) setDoc(base)
    setConflicts([])
    setSaveRows([])
    setLive('Draft discarded: showing the server state.')
  }

  const focus = (block: string | undefined) => {
    if (!block) return
    void flow.fitView({ nodes: [{ id: block }], duration: 300, maxZoom: 1.2, padding: 0.4 })
  }

  const checks: IssueRow[] = issues.map((i) => ({
    id: i.id,
    level: i.level,
    message: i.message,
    actions: [
      ...(i.block ? [{ label: 'Focus', onClick: () => focus(i.block) }] : []),
      ...(i.conflict && base
        ? [
            {
              label: 'Take server',
              onClick: () => {
                edit((d) => takeServer(d, base, i.conflict!))
                setConflicts((c) => c.filter((k) => k !== i.conflict))
              },
            },
          ]
        : []),
    ],
  }))

  // ── Links card (unchanged from the floor) ──────────────────────────────────

  const [measuring, setMeasuring] = useState<string | null>(null)
  const [measureError, setMeasureError] = useState<{ key: string; message: string } | null>(null)
  const [checkingReach, setCheckingReach] = useState<string | null>(null)
  const [reachReport, setReachReport] = useState<{ key: string; value: ReachReport } | null>(null)
  const [reachError, setReachError] = useState<{ key: string; message: string } | null>(null)
  const measure = async (a: string, b: string) => {
    const key = [a, b].sort().join('~')
    setMeasuring(key)
    setMeasureError(null)
    try {
      await backend.measureLink(a, b)
      invalidate()
    } catch (err) {
      setMeasureError({ key, message: err instanceof Error ? err.message : 'Measurement failed.' })
    } finally {
      setMeasuring(null)
    }
  }
  // No local in-flight state, unlike `measure` above, and that is the point.
  // The request returns in milliseconds while the calibration runs for
  // minutes, so a local flag would clear at once and invite a second press --
  // which the server would NOT refuse: `calibrate` and `measure` share one
  // lock, so it would block silently for minutes. The button reads
  // `edge.measuring` off the poll instead, which is also correct after a
  // reload and correct for a second person watching the same cluster.
  const [tuneError, setTuneError] = useState<{ key: string; message: string } | null>(null)
  const tune = async (a: string, b: string) => {
    const key = [a, b].sort().join('~')
    setTuneError(null)
    try {
      await backend.tuneLink(a, b)
      invalidate()
    } catch (err) {
      setTuneError({
        key,
        message: err instanceof Error ? err.message : 'Calibration failed to start.',
      })
    }
  }
  const checkReach = async (a: string, b: string) => {
    const key = [a, b].sort().join('~')
    setCheckingReach(key)
    setReachError(null)
    try {
      setReachReport({ key, value: await backend.checkReach(a, b) })
    } catch (err) {
      setReachReport(null)
      setReachError({ key, message: err instanceof Error ? err.message : 'The check failed.' })
    } finally {
      setCheckingReach(null)
    }
  }
  const name = useMemo(() => nameIndex(topology.data?.nodes ?? []), [topology.data])
  const supported = snapshot ? limitsSupported(snapshot) : false

  return (
    <div className="stage bare clusterstage">
      <GraphToolbar
        onAddModel={addDraftModel}
        onAddLimit={supported ? addRateLimit : null}
        onArrange={arrange}
        onDiscard={dirty ? discard : null}
        changes={ops.length + removedCount + relaunches.length + launches.length}
        errors={blocking}
        saving={saving}
        onSave={() => void save()}
      />
      <p className="unit wlive" aria-live="polite">
        {live}
      </p>

      <div className="card2 graphcard">
        <div className="graph">
          {active && snapshot && doc ? (
            <WiringCanvas
              blocks={blocks}
              wires={wires}
              positions={positions}
              editor={editor}
              routing={snapshot.routing}
              onMoved={moved}
              onConnect={onConnect}
              onCut={onCut}
              onRemove={onRemove}
              onMeasured={measured}
              onOpenBalancer={setDialog}
              onRemoveServed={onRemoveServed}
            />
          ) : (
            <p className="unit">{snapshot ? '' : 'Reading the cluster…'}</p>
          )}
        </div>
      </div>

      <div className="wcards">
        <div className="card2">
          <h3>Checks</h3>
          <IssueList label="Checks" issues={checks} empty="Nothing to fix." />
        </div>

        <div className="card2">
          <h3>Pending changes</h3>
          {ops.length === 0 && stopIds.length === 0 && launches.length === 0 && saveRows.length === 0 ? (
            <p className="unit">No changes. Wire something, then Save.</p>
          ) : null}
          {ops.length > 0 ? <pre className="mono wops">{JSON.stringify(ops, null, 2)}</pre> : null}
          {stopIds.length > 0 || launches.length > 0 ? (
            <ul className="wlaunches">
              {(doc?.removals.deployments ?? []).map((id) => (
                <li key={`stop:${id}`} className="unit">
                  {stopLabel(id)}
                </li>
              ))}
              {relaunches.map((r) => (
                <li key={`relaunch:${r.deployment_id}`} className="unit">
                  relaunch {r.model_id} on {r.node_ids.join(', ')} without {r.without.join(', ')}
                </li>
              ))}
              {launches.map((l, k) => (
                <li key={k} className="unit">
                  launch {l.model_id} on {(l.node_ids ?? []).join(', ')}
                  {l.parallelism?.tensor_parallel ? ` · tensor parallel ${l.parallelism.tensor_parallel}` : ''}
                  {l.speculative_tokens ? ` · MTP ×${l.speculative_tokens}` : ''}
                </li>
              ))}
            </ul>
          ) : null}
          {saveRows.length > 0 ? (
            <ul className="wsaved" aria-label="Last save">
              {saveRows.map((r) => (
                <li key={r.key} data-status={r.status}>
                  <span className="mono">{r.label}</span> — <b>{r.status}</b>
                  {r.message ? <span className="wsaved-msg">{r.message}</span> : null}
                </li>
              ))}
            </ul>
          ) : null}
          {dialog && doc && snapshot ? (
            <BalancerDialog
              served={dialog}
              doc={doc}
              snapshot={snapshot}
              newLimitId={() => `limit:${crypto.randomUUID()}`}
              onEdit={edit}
              onClose={() => setDialog(null)}
            />
          ) : null}
        </div>

        <div className="card2">
          <h3>Links</h3>
          <SelectionRail
            edges={topology.data?.edges ?? []}
            measurements={cluster.data?.links ?? []}
            measuring={measuring}
            measureError={measureError}
            onMeasure={(a, b) => void measure(a, b)}
            name={name}
            reach={{
              checking: checkingReach,
              report: reachReport,
              error: reachError,
              onCheck: (a, b) => void checkReach(a, b),
            }}
            tune={{
              error: tuneError,
              onTune: (a, b) => void tune(a, b),
              // The server's own size bands, so an evidence line is read at
              // the size its record was filed under
              // (`measurements.DECODE_COLLECTIVE_BYTES` / `BULK_COLLECTIVE_BYTES`).
              decodeBand: DECODE_BAND,
              bulkBand: BULK_BAND,
            }}
            crowded={(topology.data?.nodes.length ?? 0) > 4}
          />
        </div>

        <div className="card2">
          <h3>Sockets</h3>
          <div className="legend">
            {SOCKET_TYPE_ORDER.map((t) => (
              <span key={t}>
                <b style={{ color: SOCKET_TYPES[t].colour }}>{t}</b> {SOCKET_TYPES[t].meaning}
              </span>
            ))}
            <span>{LEGEND.shapes}</span>
            <span>{LEGEND.colours}</span>
          </div>
        </div>
      </div>
    </div>
  )
}

function rowFor(ops: WiringOp[], res: WiringResult): SaveRow {
  const op = ops[res.index]
  return {
    key: `op:${res.index}`,
    label: op ? opLabel(op) : `op ${res.index}`,
    status: res.ok ? 'done' : res.error?.code === 'not_applied' ? 'not applied' : 'failed',
    message: res.error?.message ?? null,
  }
}
