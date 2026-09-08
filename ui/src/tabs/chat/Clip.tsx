import { useEffect, useState } from 'react'
import type { SpeechResult } from '../../api/types'

/** Bytes, in the same shorthand the storage screen uses. */
function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const kib = bytes / 1024
  if (kib < 1024) return `${kib.toFixed(1)} KiB`
  return `${(kib / 1024).toFixed(2)} MiB`
}

/** One synthesised clip: the player, and what only the headers know.
 *
 *  The object URL is state rather than a `useMemo`, because it is a resource
 *  and not a derivation -- it has to be revoked, and a memo has nowhere to do
 *  that. Without the revoke, a session spent trying voices holds every clip it
 *  ever produced in memory until the tab closes.
 *
 *  `pcm` gets no player. It is raw samples with no container and no header, so
 *  nothing in the file says what rate or width to play it at and every browser
 *  refuses it -- an `<audio>` element there would render a permanently broken
 *  control. The readout says the rate instead, which is the thing a caller
 *  needs in order to do anything with those bytes.
 */
export function Clip({ clip }: { clip: SpeechResult }) {
  const [url, setUrl] = useState<string | null>(null)

  useEffect(() => {
    const next = URL.createObjectURL(clip.blob)
    setUrl(next)
    return () => {
      URL.revokeObjectURL(next)
      setUrl(null)
    }
  }, [clip])

  // `audio/pcm` is what the runtime sends for `response_format: "pcm"`
  // (runtimes/tts.py::FORMATS). Matched as a prefix so a charset parameter or
  // a provider's own spelling of the same thing does not slip past it into a
  // player that cannot decode it.
  const playable = !clip.contentType.startsWith('audio/pcm')

  return (
    <div className="clip">
      {playable && url ? (
        <audio controls src={url} style={{ width: '100%' }} />
      ) : (
        <p className="unit" style={{ margin: 0 }}>
          Raw PCM has no container, so no browser will play it. The sample rate
          below is what these bytes need to be read at.
        </p>
      )}

      <div className="cliprow">
        <span className="mono">{clip.contentType || '—'}</span>
        <span className="unit">{size(clip.bytes)}</span>
        {/* Both of these are the runtime's own headers, and a provider sends
            neither. An em dash rather than 0: nobody reported the figure,
            which is a different thing from reporting zero. */}
        <span className="unit">
          {clip.durationS === null ? '—' : `${clip.durationS.toFixed(2)} s`}
        </span>
        <span className="unit">
          {clip.sampleRate === null ? '—' : `${clip.sampleRate.toLocaleString()} Hz`}
        </span>
        <span style={{ flex: 1 }} />
        <span className="unit mono">{clip.requestId ?? '—'}</span>
      </div>
    </div>
  )
}
