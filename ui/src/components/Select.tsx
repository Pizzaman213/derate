import { useEffect, useId, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react'
import { firstIndex, lastIndex, moveActive, typeaheadIndex } from './listNav'
import { useDismiss } from './useDismiss'

export interface SelectOption<T extends string> {
  value: T
  label: string
}

interface Props<T extends string> {
  id?: string
  'aria-label'?: string
  value: T
  options: readonly SelectOption<T>[]
  onChange: (value: T) => void
  disabled?: boolean
  className?: string
  style?: CSSProperties
}

// A typed character resets the buffer after this many idle milliseconds --
// mirrors how a native <select> forgets what you were typing once you pause.
const TYPEAHEAD_IDLE_MS = 500

/** A closed-choice dropdown: the replacement for a plain `<select>`, styled to
 *  match the rest of the UI instead of the browser's own chrome.
 *
 *  The WAI-ARIA "select-only combobox" pattern: the trigger carries
 *  `role="combobox"` and keeps DOM focus for the whole interaction -- arrow
 *  keys, Home/End and letter-typeahead all move `aria-activedescendant`
 *  rather than moving focus into the listbox, so Tab always just leaves (and
 *  `useDismiss`'s blur handler closes the panel as a side effect) with no
 *  focus trap to build or maintain.
 *
 *  Arrow keys and Home/End STAGE a choice while open -- they move the active
 *  row, never call `onChange` -- and only Enter, Space or a click COMMIT it.
 *  Escape is therefore a pure cancel: there is nothing to revert, because
 *  nothing was written yet. Closed, though, Home/End and letter-typeahead
 *  commit immediately, matching what a native `<select>` does when you type
 *  or press Home/End without opening it first.
 *
 *  Dismissal (`pointerdown` outside, `Escape`, focus truly leaving) is
 *  `useDismiss.ts`, shared unmodified with `Combobox.tsx` -- see that file for
 *  why this is shared and the keyboard/commit logic below is not. */
export function Select<T extends string>({
  id,
  'aria-label': ariaLabel,
  value,
  options,
  onChange,
  disabled,
  className,
  style,
}: Props<T>) {
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const wrapRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const listId = useId()
  const buffer = useRef('')
  const lastKeyAt = useRef(0)

  const currentIndex = options.findIndex((o) => o.value === value)
  const current = currentIndex >= 0 ? options[currentIndex] : null

  const close = () => setOpen(false)
  const dismiss = useDismiss(open, close, wrapRef, triggerRef)

  const openList = () => {
    setOpen(true)
    setActiveIndex(currentIndex >= 0 ? currentIndex : firstIndex(options.length))
  }

  const commit = (index: number) => {
    const opt = index >= 0 && index < options.length ? options[index] : undefined
    if (opt && opt.value !== value) onChange(opt.value)
    close()
    triggerRef.current?.focus()
  }

  // Scroll the active row into view as it changes -- by keyboard, not by
  // measuring, since the panel is never portalled and stays a plain
  // descendant of the wrapper.
  useEffect(() => {
    if (!open || activeIndex < 0) return
    document.getElementById(`${listId}-opt-${activeIndex}`)?.scrollIntoView({ block: 'nearest' })
  }, [open, activeIndex, listId])

  const typeahead = (char: string) => {
    const now = Date.now()
    const isRepeat = buffer.current.length > 0 && [...buffer.current].every((c) => c === char)
    if (now - lastKeyAt.current > TYPEAHEAD_IDLE_MS || isRepeat) {
      buffer.current = char
    } else {
      buffer.current += char
    }
    lastKeyAt.current = now
    // A single repeated letter cycles from the current row; a growing prefix
    // searches the whole list from the top.
    const from = buffer.current.length === 1 ? (open ? activeIndex : currentIndex) : -1
    const idx = typeaheadIndex(options, (o) => o.label, buffer.current, from)
    if (idx === -1) return
    if (open) setActiveIndex(idx)
    else commit(idx)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (disabled) return
    const count = options.length
    switch (e.key) {
      case 'ArrowDown':
      case 'ArrowUp':
        e.preventDefault()
        if (!open) openList()
        else setActiveIndex((i) => moveActive(i, count, e.key === 'ArrowDown' ? 1 : -1))
        return
      case 'Home':
        e.preventDefault()
        if (open) setActiveIndex(firstIndex(count))
        else commit(firstIndex(count))
        return
      case 'End':
        e.preventDefault()
        if (open) setActiveIndex(lastIndex(count))
        else commit(lastIndex(count))
        return
      case 'Enter':
      case ' ':
        e.preventDefault()
        if (open) commit(activeIndex)
        else openList()
        return
      default:
        if (e.key.length === 1 && /\S/.test(e.key)) typeahead(e.key)
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
      <button
        ref={triggerRef}
        id={id}
        type="button"
        className="ddtrigger"
        disabled={disabled}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open && activeIndex >= 0 ? `${listId}-opt-${activeIndex}` : undefined}
        aria-label={ariaLabel}
        onClick={() => (open ? close() : openList())}
      >
        <span>{current?.label ?? value}</span>
        <span className="ddchevron" aria-hidden>
          &#9662;
        </span>
      </button>
      {open ? (
        <div className="ddpanel" id={listId} role="listbox">
          {options.map((o, i) => (
            <div
              key={o.value}
              id={`${listId}-opt-${i}`}
              role="option"
              aria-selected={o.value === value}
              className={['ddopt', i === activeIndex ? 'active' : ''].filter(Boolean).join(' ')}
              title={o.label}
              onMouseEnter={() => setActiveIndex(i)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => commit(i)}
            >
              {o.label}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}
