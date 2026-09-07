import { useEffect, useRef, useState } from 'react'

/** A command block with a copy button.
 *
 *  New ground: nothing in this UI copied to the clipboard before, because
 *  nothing in it was meant to be run somewhere else. The install command is,
 *  and it is long enough that retyping it is not an option.
 *
 *  `navigator.clipboard` is not enough on its own here. The gateway binds to
 *  the LAN and is served over plain HTTP, so on `http://spark-01:8080` — the
 *  address an operator actually opens — the Clipboard API is undefined
 *  outside a secure context. `localhost` is the one origin where it works,
 *  which is exactly the origin a developer tests on and nobody deploys to.
 *  The `execCommand` fallback is therefore the path that runs in production,
 *  not the legacy one.
 *
 *  The text is also selectable and the block is scrollable, so a browser that
 *  refuses both paths still leaves the operator able to select and copy by
 *  hand. There is no toast system in this UI, so the button reports into
 *  itself and reverts.
 */
export function Copyable({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => () => window.clearTimeout(timer.current), [])

  const report = (next: 'copied' | 'failed') => {
    setState(next)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setState('idle'), 2000)
  }

  const copy = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
        report('copied')
        return
      }
    } catch {
      // Present but refused — a permissions policy, or a document that is not
      // focused. Fall through rather than reporting a failure the fallback
      // may not have.
    }
    report(legacyCopy(text) ? 'copied' : 'failed')
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

/** The path that actually runs on a plain-HTTP LAN address. */
function legacyCopy(text: string): boolean {
  const area = document.createElement('textarea')
  area.value = text
  // Off-screen rather than hidden: a display:none or visibility:hidden element
  // cannot be selected, and the copy silently does nothing.
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.top = '-1000px'
  area.style.opacity = '0'
  document.body.appendChild(area)
  try {
    area.select()
    area.setSelectionRange(0, text.length)
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(area)
  }
}
