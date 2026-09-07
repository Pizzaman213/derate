import type { HistoryEnvelope } from '../../api/types'
import type { Resource } from '../../state/backend'
import { Verbatim } from '../../components/Verbatim'
import { bucketSeconds, type HistoryWindow } from '../../state/history'

/** Where the numbers above this line came from.
 *
 *  This is the point of the history envelope and the reason it is not just a
 *  list of samples. A flat line has two causes -- the machine was idle, or the
 *  rows were never collected -- and the product already refuses to blur that
 *  distinction for a link it has never measured. So: which resolution answered,
 *  whether it survives a restart, whether the far end of the window was
 *  dropped, and every known hole spelled out in the archive's own words. */
export function Provenance({
  window,
  resource,
}: {
  window: HistoryWindow
  resource: Resource<HistoryEnvelope>
}) {
  if (window === 'live') {
    return (
      <div className="unit">
        Live. Accumulated in this browser from the 1 Hz frame since the page
        loaded, so it starts empty and a reload loses it.
      </div>
    )
  }

  if (resource.error) {
    // The server's own sentence. Telemetry is off when its data root does not
    // exist, which is how the container records and a development machine does
    // not, and the 503 says exactly that -- better than anything written here.
    return <Verbatim text={resource.error.message} size="unit" />
  }

  const env = resource.data
  if (!env) return <div className="unit">Reading…</div>

  const per = bucketSeconds(env.resolution)
  const shape =
    env.resolution === 'ring'
      ? 'the coordinator’s five-minute memory buffer'
      : per === 60
        ? 'one-minute buckets'
        : per === 3600
          ? 'one-hour buckets'
          : 'raw samples'

  return (
    <div className="unit">
      {shape}
      {env.durable
        ? '. Read from the durable archive, so it survives a restart.'
        : '. Held in memory only, so a restart loses it — no archive is being kept on this coordinator.'}
      {env.truncated
        ? ' The window was wider than one answer can carry, so the oldest end of it is not shown.'
        : ''}
      {env.gaps.length > 0 ? (
        <>
          {' '}
          {env.gaps.length === 1 ? 'One stretch is' : `${env.gaps.length} stretches are`} known
          missing rather than quiet:{' '}
          {env.gaps
            .map((g) => `${g.reason} (${Math.round((g.to_ts - g.from_ts) / 60)} min)`)
            .join('; ')}
          .
        </>
      ) : null}
    </div>
  )
}
