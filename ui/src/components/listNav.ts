// Pure index and filter arithmetic shared by Select.tsx and Combobox.tsx.
//
// Kept separate from both components because `moveActive` is identical for
// each of them and the rest are cheap, pure and worth a hermetic check --
// nothing here touches the DOM, so listNav.check.mjs can assert it directly
// rather than trusting a green tsc. The two components still own their own
// keyboard *policy* (when to open, when to commit, how a typeahead buffer
// grows vs cycles) -- this file only answers "given this state, what index
// comes next", never "what should happen".

/** One step of arrow-key movement, clamped at both ends. Never wraps: an open
 *  native `<select>` in Chromium does not cycle back to the top when you
 *  arrow past the last row, and neither does this. `current: -1` (nothing
 *  active yet) moves to the first row on ArrowDown, the last on ArrowUp. */
export function moveActive(current: number, count: number, delta: number): number {
  if (count === 0) return -1
  if (current < 0) return delta > 0 ? 0 : count - 1
  return Math.max(0, Math.min(count - 1, current + delta))
}

export function firstIndex(count: number): number {
  return count > 0 ? 0 : -1
}

export function lastIndex(count: number): number {
  return count > 0 ? count - 1 : -1
}

/** Cyclic prefix search for letter-typeahead, starting just after `from` and
 *  wrapping once all the way around. Case-insensitive; `-1` if nothing
 *  matches. The caller decides what `from` means: `-1` searches the whole
 *  list from the top (a freshly-typed, growing prefix), the current index
 *  cycles through repeat presses of one letter -- both are native `<select>`
 *  behaviors, and the difference is buffer policy, not this function. */
export function typeaheadIndex<T>(
  options: readonly T[],
  labelOf: (o: T) => string,
  query: string,
  from: number,
): number {
  const q = query.toLowerCase()
  const n = options.length
  if (!q || n === 0) return -1
  for (let step = 1; step <= n; step++) {
    const i = (from + step) % n
    const option = options[i]
    if (option !== undefined && labelOf(option).toLowerCase().startsWith(q)) return i
  }
  return -1
}

/** Case-insensitive substring filter for Combobox's suggestion list. An empty
 *  query returns every item, unfiltered -- that is what a freshly-focused
 *  field with nothing typed yet should offer. */
export function filterBySubstring(items: readonly string[], query: string): string[] {
  const q = query.trim().toLowerCase()
  if (!q) return [...items]
  return items.filter((item) => item.toLowerCase().includes(q))
}

/** Caps a suggestion list at `max` and reports how many were cut, so a
 *  several-hundred-row catalogue renders as "a list you type into" (per
 *  ProviderBackupPanel's own reasoning) rather than one you scroll. */
export function capMatches<T>(
  items: readonly T[],
  max: number,
): { shown: T[]; hiddenCount: number } {
  if (items.length <= max) return { shown: [...items], hiddenCount: 0 }
  return { shown: items.slice(0, max), hiddenCount: items.length - max }
}
