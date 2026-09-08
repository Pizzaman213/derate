import { useActivity } from '../state/resources'
import { activityRows } from './activity'
import { Lamp } from '../components/Lamp'
import { ProportionBar } from '../components/Bars'
import { Verbatim } from '../components/Verbatim'
import { relativeTime } from '../format'

/** What is arriving: transfers in flight, and models still starting.
 *
 *  The rail's other four sections describe a cluster that has settled. Three of
 *  them say "Nothing is being served." and they said it while a download was
 *  running, which is how a pull came to be minutes of total silence after one
 *  sentence — and how a launch came to be up to half an hour of `launching`
 *  with no number anywhere on the screen.
 *
 *  It renders only when there is something to report. A fifth permanent
 *  "Nothing is happening." line beneath the three that already say it makes
 *  the rail worse, not better; the section appearing IS the signal.
 */
export function ActivitySection() {
  const activity = useActivity()
  const rows = activityRows(activity.data)

  if (rows.length === 0) return null

  return (
    <section>
      <h2>Activity</h2>
      <div style={{ display: 'grid', gap: 12 }}>
        {rows.map((row) => (
          <div key={row.key} style={{ display: 'grid', gap: 4 }}>
            <div className="row" style={{ padding: '0 0 3px' }}>
              <span className="label" style={{ fontWeight: 500 }}>
                {row.title}
              </span>
              <span
                className="mono unit"
                style={{ display: 'flex', alignItems: 'center', gap: 6 }}
              >
                <Lamp
                  signal={row.signal}
                  label={`${row.title} ${row.status || row.kind}`}
                />
              </span>
            </div>

            {row.target ? (
              <div className="unit">
                {row.kind === 'download' ? 'to ' : 'on '}
                {row.target}
              </div>
            ) : null}

            <ProportionBar
              value={row.value}
              tone={row.signal === 'fault' ? 'fault' : 'ink'}
              label={
                row.value == null
                  ? // Said out loud rather than left to a dashed track, because
                    // a screen reader gets no picture at all.
                    `${row.title} is working, with no progress figure to report`
                  : row.kind === 'download'
                    ? `${Math.round(row.value * 100)} percent of ${row.title} downloaded`
                    : // A launch's only figure is the runtime counting its
                      // checkpoint shards onto the GPU. Reading that out as
                      // "downloaded" would name the step that already finished.
                      `${Math.round(row.value * 100)} percent of ${row.title} loaded onto the GPU`
              }
            />

            {/* The server's own word for what it is doing — "pulling manifest",
                "verifying sha256 digest", "launching". Passed through, never
                re-cased or shortened: it is more precise than any rewrite. */}
            {row.status ? <Verbatim text={row.status} size="unit" /> : null}

            <div className="unit" style={{ marginTop: 2 }}>
              {row.detail}
              {row.kind === 'launch' && row.since
                ? ` · since ${relativeTime(row.since)}`
                : ''}
            </div>

            {row.error ? (
              <Verbatim text={row.error} size="unit" />
            ) : null}
          </div>
        ))}
      </div>
    </section>
  )
}
