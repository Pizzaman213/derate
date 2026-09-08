import { useState } from 'react'
import { applyTheme, loadTheme, type Theme } from '../../theme'

// Moved out of the header (shell/Header.tsx): a colour scheme is a setting,
// not a destination control, and the header no longer has it.
export function AppearanceCard() {
  const [theme, setTheme] = useState<Theme>(loadTheme)

  return (
    <div className="card2">
      <h3>Appearance</h3>
      <div className="row">
        <span>Colour scheme</span>
        <select
          value={theme}
          onChange={(e) => {
            const t = e.target.value as Theme
            setTheme(t)
            applyTheme(t)
          }}
        >
          <option value="system">System</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </div>
    </div>
  )
}
