import { useState } from 'react'
import { Icon } from './Icon'

export type PasswordFieldProps = {
  id?: string; name?: string
  value: string; onChange: (value: string) => void
  autoComplete: 'current-password' | 'new-password'
  autoFocus?: boolean; disabled?: boolean
  /** Class for the <input> itself (setup/login pass "setup-field"). */
  inputClassName?: string; className?: string
  'aria-invalid'?: boolean; 'aria-describedby'?: string
}

/** A password input with a show/hide toggle inside its right edge. The
 *  toggle swaps the input's `type` only, so the value and the caret survive
 *  it. Spec: .omp/design/DESIGN.md#passwordfield. */
export function PasswordField({
  id, name, value, onChange, autoComplete, autoFocus, disabled,
  inputClassName, className,
  'aria-invalid': ariaInvalid, 'aria-describedby': ariaDescribedBy,
}: PasswordFieldProps) {
  const [shown, setShown] = useState(false)
  return (
    <span className={'pwfield' + (className ? ' ' + className : '')}>
      <input
        id={id}
        name={name}
        className={'pwfield-input' + (inputClassName ? ' ' + inputClassName : '')}
        type={shown ? 'text' : 'password'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        autoFocus={autoFocus}
        disabled={disabled}
        aria-invalid={ariaInvalid}
        aria-describedby={ariaDescribedBy}
      />
      <button
        type="button"
        className="ghost pwfield-toggle"
        aria-pressed={shown}
        aria-label={shown ? 'Hide password' : 'Show password'}
        disabled={disabled}
        onClick={() => setShown((s) => !s)}
      >
        <Icon name={shown ? 'eye-off' : 'eye'} decorative />
      </button>
    </span>
  )
}
