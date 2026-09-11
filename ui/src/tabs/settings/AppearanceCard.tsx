import { useState } from 'react'
import { Select, type SelectOption } from '../../components/Select'
import { applyTheme, loadTheme, type Theme } from '../../theme'

const THEME_OPTIONS: SelectOption<Theme>[] = [
  { value: 'system', label: 'System' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
]

// Moved out of the header (shell/Header.tsx): a colour scheme is a setting,
// not a destination control, and the header no longer has it.
export function AppearanceCard() {
  const [theme, setTheme] = useState<Theme>(loadTheme)

  const change = (t: Theme) => {
    setTheme(t)
    applyTheme(t)
  }

  return (
    <div className="card2">
      <h3>Appearance</h3>
      <div className="row">
        {/* No `<label htmlFor>` existed on the old bare `<select>` either --
            `aria-label` here is the first accessible name this control has
            had, not a behavior change. */}
        <span>Colour scheme</span>
        <Select aria-label="Colour scheme" value={theme} options={THEME_OPTIONS} onChange={change} />
      </div>
    </div>
  )
}
