export type Theme = 'system' | 'light' | 'dark'

const KEY = 'derate.theme'

export function loadTheme(): Theme {
  return (localStorage.getItem(KEY) as Theme) ?? 'system'
}

/** Dark mode is a token swap. Nothing here touches a component. */
export function applyTheme(theme: Theme) {
  const root = document.documentElement
  if (theme === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
  localStorage.setItem(KEY, theme)
}
