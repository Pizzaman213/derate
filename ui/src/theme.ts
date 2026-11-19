export const THEMES = ['system', 'light', 'dark', 'high-contrast'] as const
export type Theme = (typeof THEMES)[number]

const KEY = 'derate.theme'

/** Whatever is stored is validated against the list: a value written by an
 *  older build (or by hand) that is not a theme falls back to `system` rather
 *  than being set as a `data-theme` nothing in tokens.css answers to. */
export function loadTheme(): Theme {
  const stored = localStorage.getItem(KEY)
  return (THEMES as readonly string[]).includes(stored ?? '') ? (stored as Theme) : 'system'
}

/** A theme is a token swap. Nothing here touches a component. `system`
 *  removes the attribute so `prefers-color-scheme` and `prefers-contrast`
 *  decide; the other three pin it. */
export function applyTheme(theme: Theme) {
  const root = document.documentElement
  if (theme === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
  localStorage.setItem(KEY, theme)
}
