import { useId, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react'
import { capMatches, filterBySubstring, moveActive } from './listNav'
import { useDismiss } from './useDismiss'

interface Props {
  id?: string
  'aria-label'?: string
  value: string
  onChange: (value: string) => void
  suggestions: readonly string[]
  placeholder?: string
  disabled?: boolean
  spellCheck?: boolean
  autoComplete?: string
  /** Caps how many filtered suggestions render, so a several-hundred-row
   *  catalogue stays "a list you type into, not one you scroll" (the
   *  reasoning `ProviderBackupPanel.tsx` already names) instead of turning
   *  into a scroll-through-everything panel. */
  maxVisible?: number
  className?: string
  style?: CSSProperties
}

const DEFAULT_MAX_VISIBLE = 50

/** A free-text field with filtered suggestions: the replacement for a plain
 *  `<input list>` + `<datalist>` pair, styled to match the rest of the UI.
 *
 *  Always plain text with arbitrary values allowed -- unlike `Select.tsx`,
 *  there is no staging step, because every keystroke already IS the value,
 *  exactly like the `<input>` this replaces. `aria-autocomplete="list"`
 *  rather than `"both"`: `"both"` auto-completes inline into the field and
 *  would silently mutate what was typed, which is the one thing a field that
 *  promises arbitrary values must never do.
 *
 *  Arrow keys move the highlighted suggestion without touching the typed
 *  text. Enter with a suggestion highlighted commits it; Enter with nothing
 *  highlighted leaves the typed text exactly as it was -- the detail that
 *  keeps "arbitrary values allowed" real, not just documented. Home/End and
 *  Left/Right are left alone: this control's trigger is a real text input,
 *  not a button, so those keys keep their native cursor-movement meaning
 *  instead of being repurposed the way `Select.tsx` repurposes them on its
 *  button trigger.
 *
 *  Dismissal is `useDismiss.ts`, shared unmodified with `Select.tsx`. */
export function Combobox({
  id,
  'aria-label': ariaLabel,
  value,
  onChange,
  suggestions,
  placeholder,
  disabled,
  spellCheck = false,
  autoComplete,
  maxVisible = DEFAULT_MAX_VISIBLE,
  className,
  style,
}: Props) {
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const wrapRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listId = useId()

  const close = () => setOpen(false)
  const dismiss = useDismiss(open, close, wrapRef, inputRef)

  const matches = filterBySubstring(suggestions, value)
  const { shown, hiddenCount } = capMatches(matches, maxVisible)

  const commit = (suggestion: string) => {
    onChange(suggestion)
    close()
    inputRef.current?.focus()
  }

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (disabled) return
    switch (e.key) {
      case 'ArrowDown':
      case 'ArrowUp':
        e.preventDefault()
        if (!open) setOpen(true)
        setActiveIndex((i) => moveActive(i, shown.length, e.key === 'ArrowDown' ? 1 : -1))
        return
      case 'Enter': {
        if (!open) return
        e.preventDefault()
        const active = activeIndex >= 0 ? shown[activeIndex] : undefined
        if (active !== undefined) commit(active)
        else close()
      }
    }
  }

  return (
    <div
      className={['ddwrap', className].filter(Boolean).join(' ')}
      style={style}
      ref={wrapRef}
      onKeyDown={(e) => {
        dismiss.onKeyDown(e)
        if (e.key !== 'Escape') onKeyDown(e)
      }}
      onBlur={dismiss.onBlur}
    >
      <input
        ref={inputRef}
        id={id}
        type="text"
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        spellCheck={spellCheck}
        autoComplete={autoComplete}
        aria-label={ariaLabel}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open && activeIndex >= 0 ? `${listId}-opt-${activeIndex}` : undefined}
        onChange={(e) => {
          onChange(e.target.value)
          setActiveIndex(-1)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
      />
      {open ? (
        <div className="ddpanel" id={listId} role="listbox">
          {shown.length === 0 ? (
            <div className="ddopt off">No matches — typed values are used as-is.</div>
          ) : (
            shown.map((s, i) => (
              <div
                key={s}
                id={`${listId}-opt-${i}`}
                role="option"
                aria-selected={s === value}
                className={['ddopt', i === activeIndex ? 'active' : ''].filter(Boolean).join(' ')}
                title={s}
                onMouseEnter={() => setActiveIndex(i)}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => commit(s)}
              >
                {s}
              </div>
            ))
          )}
          {hiddenCount > 0 ? (
            <div className="ddfoot">{hiddenCount} more — keep typing to narrow</div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
