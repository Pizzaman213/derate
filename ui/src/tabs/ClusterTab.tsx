import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReachReport } from '../api/types'
import type { Point } from './cluster/layout'
import { useCluster, useProviders, useRouting, useTopology } from '../state/resources'
import { useBackend } from '../state/backend'
import { nameIndex } from '../state/names'
import { ClusterGraph, type ClusterGraphHandle } from './cluster/ClusterGraph'
import { GraphToolbar } from './cluster/GraphToolbar'
import { SelectionRail } from './cluster/SelectionRail'
import {
  clearArrangement,
  isArranged,
  readArrangement,
  writeArrangement,
  NO_ARRANGEMENT,
  type Arrangement,
} from './cluster/order'

const EMPTY_TOPOLOGY = { cluster_id: '', coordinator: '', nodes: [], edges: [], deployments: [] }

/** The Cluster destination: the machine floor and the rail beneath it.
 *
 *  This component fetches its own data (the same `state/resources` hooks every
 *  other destination uses) rather than taking props, so it can be dropped into
 *  AppShell's `cluster` tabpanel on its own. */
/** The two size bands the server files NCCL records under
 *  (`measurements.DECODE_COLLECTIVE_BYTES` / `BULK_COLLECTIVE_BYTES`, run
 *  through `measurements.band`). Structural rather than fetched: the decode
 *  all-reduce a TP step issues is `hidden_size x 2 x batch` and the prefill
 *  one is megabytes. */
const DECODE_BAND = 8192
const BULK_BAND = 4194304

export function ClusterTab() {
  const cluster = useCluster()
  const topology = useTopology()
  const routing = useRouting()
  const providers = useProviders()
  const { backend, invalidate } = useBackend()

  const [measuring, setMeasuring] = useState<string | null>(null)
  const [measureError, setMeasureError] = useState<{ key: string; message: string } | null>(null)
  // The reachability check is separate state from the measurement above, and
  // deliberately so: they are different questions at different prices, and one
  // running must not grey out the other's button.
  // Only an error, deliberately. See `tune` below: the request returns in
  // milliseconds and the work runs for minutes, so a local in-flight flag
  // would be wrong almost immediately.
  const [tuneError, setTuneError] = useState<{ key: string; message: string } | null>(null)
  const [checking, setChecking] = useState<string | null>(null)
  const [reachReport, setReachReport] = useState<{ key: string; value: ReachReport } | null>(null)
  const [reachError, setReachError] = useState<{ key: string; message: string } | null>(null)
  const graphRef = useRef<ClusterGraphHandle>(null)
  const zoomLabelRef = useRef<HTMLSpanElement>(null)

  const clusterId = topology.data?.cluster_id ?? ''
  // Which slot each machine is dealt, and how far it has been dragged off it.
  // One piece of state because they are one preference: the floor's
  // arrangement, saved the moment it changes and restored on the next visit.
  const [arrangement, setArrangement] = useState<Arrangement>(NO_ARRANGEMENT)
  const [live, setLive] = useState('')

  /** The current arrangement, readable from the two writers below without
   *  putting it in their dependency arrays. Both are handed to `ClusterGraph`,
   *  which keys its pointer and key listeners on them -- a new identity per
   *  drop would tear those listeners down and rebuild them on every move. */
  const arrangementRef = useRef(arrangement)
  arrangementRef.current = arrangement

  // The arrangement is per cluster, and the cluster id is not known until the
  // first poll answers.
  useEffect(() => {
    setArrangement(clusterId ? readArrangement(clusterId) : NO_ARRANGEMENT)
  }, [clusterId])

  // Every write goes through here, so "auto-saves" is a property of the state
  // rather than something each caller has to remember: there is no path that
  // changes the floor on screen without the store agreeing with it.
  const save = useCallback(
    (next: Arrangement) => {
      setArrangement(next)
      if (clusterId) writeArrangement(clusterId, next)
    },
    [clusterId],
  )

  /** A keyboard reorder hands back the RECONCILED arrangement -- present
   *  machines only -- but the stored order keeps departed ids so a machine
   *  that leaves and rejoins returns to the slot somebody put it in
   *  (order.ts). Overwriting the store with the present-only list silently
   *  dropped those slots, so each departed id is spliced back in at the
   *  position it held. */
  const reorder = useCallback(
    (order: string[]) => {
      const stored = arrangementRef.current.order
      const merged = [...order]
      if (stored) {
        const present = new Set(order)
        for (const id of stored.filter((v) => !present.has(v))) {
          merged.splice(Math.min(stored.indexOf(id), merged.length), 0, id)
        }
      }
      save({ ...arrangementRef.current, order: merged })
    },
    [save],
  )

  const move = useCallback(
    (nodeId: string, offset: Point | null) => {
      const offsets = { ...arrangementRef.current.offsets }
      if (offset) offsets[nodeId] = offset
      else delete offsets[nodeId]
      save({ ...arrangementRef.current, offsets })
    },
    [save],
  )

  const resetLayout = useCallback(() => {
    setArrangement(NO_ARRANGEMENT)
    if (clusterId) clearArrangement(clusterId)
    setLive('Machines put back where they started.')
  }, [clusterId])

  const measure = async (a: string, b: string) => {
    const key = [a, b].sort().join('~')
    setMeasuring(key)
    setMeasureError(null)
    try {
      await backend.measureLink(a, b)
      invalidate()
    } catch (err) {
      // A rejected measurement must surface somewhere, not vanish into an
      // unhandled promise rejection while the button silently re-enables
      // and the rail still reads "Never measured" with no explanation.
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
    setChecking(key)
    setReachError(null)
    try {
      setReachReport({ key, value: await backend.checkReach(a, b) })
    } catch (err) {
      // A refused check has to say so where the button is. The alternative --
      // the button re-enabling with nothing new on screen -- reads as "checked,
      // and everything is fine", which is the one thing it does not mean.
      setReachReport(null)
      setReachError({ key, message: err instanceof Error ? err.message : 'The check failed.' })
    } finally {
      setChecking(null)
    }
  }

  // One naming rule for the whole destination, so a plate and the chip naming
  // the same machine cannot disagree.
  const name = useMemo(() => nameIndex(topology.data?.nodes ?? []), [topology.data])

  const nodeCount = topology.data?.nodes.length ?? 0

  return (
    <div className="stage bare clusterstage">
      <GraphToolbar
        graphRef={graphRef}
        zoomLabelRef={zoomLabelRef}
        rearranged={isArranged(arrangement)}
        onResetLayout={resetLayout}
      />

      {/* The drawing takes whatever is left between the toolbar and the two
          reference blocks below, and sits in the middle of it. `.floorcard`
          is what carries the growing, because the card is now the flex item
          and the floor inside it is not. */}
      <div className="card2 floorcard">
        <div className="floor">
          <ClusterGraph
            ref={graphRef}
            deployments={cluster.data?.deployments ?? []}
            deploymentsKnown={cluster.data != null}
            topology={topology.data ?? EMPTY_TOPOLOGY}
            nodes={cluster.data?.nodes ?? []}
            routing={routing.data ?? []}
            providers={providers.data ?? []}
            zoomLabelRef={zoomLabelRef}
            order={arrangement.order}
            onReorder={reorder}
            offsets={arrangement.offsets}
            onMove={move}
            announce={setLive}
          />
        </div>
      </div>

      <p aria-live="polite" className="sr-only">
        {live}
      </p>

      {/* The rail and the legend are the reference desk, so .clusterstage pins
          them to the bottom of the destination instead of letting them trail
          the drawing. Nothing above them moves when a link is selected.

          Each is its own block rather than a hairline rule under the drawing:
          three regions on one bare surface read as one thing with dividers in
          it, and every other surface in the product -- Storage, Settings, the
          sidebar's sections -- is a flat list of separate blocks. The rule was
          already there and this destination was the exception. */}
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
            checking,
            report: reachReport,
            error: reachError,
            onCheck: (a, b) => void checkReach(a, b),
          }}
          tune={{
            error: tuneError,
            onTune: (a, b) => void tune(a, b),
            // The server's own size bands, so an evidence line is read at the
            // size its record was filed under.
            decodeBand: DECODE_BAND,
            bulkBand: BULK_BAND,
          }}
          crowded={nodeCount > 4}
        />
      </div>

      {/* One meaning per channel. Width is measured bandwidth; a dash is
          never-measured and nothing else now that the routing boundary is
          gone; colour means something is wrong, which is why a healthy plate
          has none -- with the traffic blocks below as the one deliberate
          exception, and they are traffic, not state. */}
      <div className="card2">
        <h3>Legend</h3>
        <div className="legend">
          <span>
            <b>thick</b> measured all-reduce, against the 40 GB/s tensor-parallel threshold
          </span>
          <span>
            <b>dashed</b> never measured, and carrying no figure
          </span>
          <span>
            <b>inset meter</b> memory used
          </span>
          <span>
            <b>plate border</b> amber or red when the machine needs looking at
          </span>
          <span>
            <b>band</b> one deployment across the machines it occupies
          </span>
          <span>
            <b>provider logo</b> each row's own mark — the one thing on that box that still isn't yours
          </span>
          <span>
            <b>blue</b> one block, one request in flight
          </span>
          <span>
            <b>teal / violet</b> output tokens leaving — teal when this name's last 15 minutes of
            requests mostly streamed, violet when they mostly did not, grey when nothing recorded
          </span>
        </div>
      </div>
    </div>
  )
}
