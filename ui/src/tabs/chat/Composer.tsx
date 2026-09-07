import { useState, type KeyboardEvent } from 'react'

interface Props {
  /** No model selected, or the one selected cannot serve. */
  disabled: boolean
  busy: boolean
  onSend: (text: string) => void
  onStop: () => void
  onClear: () => void
  canClear: boolean
}

export function Composer({ disabled, busy, onSend, onStop, onClear, canClear }: Props) {
  const [text, setText] = useState('')

  const send = () => {
    const body = text.trim()
    if (!body || disabled || busy) return
    setText('')
    onSend(body)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter breaks the line. The composer is the only
    // multi-line input in the product, so it says so under the field rather
    // than assuming the convention is obvious.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="composer">
      <label className="sr-only" htmlFor="chat-input">
        Message
      </label>
      <textarea
        id="chat-input"
        value={text}
        disabled={disabled}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={disabled ? 'Pick a model first' : 'Say something'}
        rows={3}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-2)' }}>
        <span className="unit">Enter sends · Shift+Enter for a new line</span>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClear} disabled={!canClear || busy}>
          Clear
        </button>
        {busy ? (
          <button type="button" onClick={onStop}>
            Stop
          </button>
        ) : (
          <button type="button" onClick={send} disabled={disabled || !text.trim()}>
            Send
          </button>
        )}
      </div>
    </div>
  )
}
