import { useEffect, useRef, useState } from 'react'
import { copyToClipboard } from './clipboard'

/** A command block with a copy button.
 *
 *  New ground: nothing in this UI copied to the clipboard before, because
 *  nothing in it was meant to be run somewhere else. The install command is,
 *  and it is long enough that retyping it is not an option.
 *
 *  There is no toast system in this UI, so the button reports into itself and
 *  reverts. The clipboard strategy itself lives in `clipboard.ts`, shared with
 *  the chat transcript's own copy buttons.
 *
 *  The text is also selectable and the block is scrollable, so a browser that
 *  refuses both paths still leaves the operator able to select and copy by
 *  hand.
 */
export function Copyable({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => () => window.clearTimeout(timer.current), [])

  const copy = async () => {
    const result = await copyToClipboard(text)
    setState(result)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setState('idle'), 2000)
  }

  return (
    <div>
      <div className="cmd">{text}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 6 }}>
        <button style={{ padding: '3px 9px' }} onClick={() => void copy()}>
          {state === 'copied' ? 'Copied' : label}
        </button>
        {state === 'failed' ? (
          <span className="unit" style={{ color: 'var(--warn)' }}>
            This browser would not copy it. Select the line above instead.
          </span>
        ) : null}
      </div>
    </div>
  )
}
