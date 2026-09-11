// Verifier for listNav.ts -- the index/filter arithmetic Select.tsx and
// Combobox.tsx both build their keyboard handling on. Pure and DOM-free, so
// this is a real hermetic check rather than the manual-only standard the
// components themselves are held to (there is no browser-DOM test runner in
// this repo for keyboard/focus behavior).
//
//   cd ui && node src/components/listNav.check.mjs
import { load, report } from '../check/harness.mjs'

const M = await load(import.meta.url, './listNav.ts')
const { check, done } = report()

// ── moveActive: clamped, never wraps ─────────────────────────────────────────

check(M.moveActive(-1, 0, 1) === -1, 'an empty list has no active index')
check(M.moveActive(-1, 5, 1) === 0, 'ArrowDown from nothing active lands on the first row')
check(M.moveActive(-1, 5, -1) === 4, 'ArrowUp from nothing active lands on the last row')
check(M.moveActive(0, 5, -1) === 0, 'ArrowUp at the first row stays put, does not wrap')
check(M.moveActive(4, 5, 1) === 4, 'ArrowDown at the last row stays put, does not wrap')
check(M.moveActive(2, 5, 1) === 3, 'ArrowDown from the middle moves by one')
check(M.moveActive(2, 5, -1) === 1, 'ArrowUp from the middle moves by one')

// ── firstIndex / lastIndex ────────────────────────────────────────────────────

check(M.firstIndex(0) === -1, 'firstIndex of an empty list is -1')
check(M.firstIndex(5) === 0, 'firstIndex is 0')
check(M.lastIndex(0) === -1, 'lastIndex of an empty list is -1')
check(M.lastIndex(5) === 4, 'lastIndex is count - 1')

// ── typeaheadIndex: cyclic prefix search ─────────────────────────────────────

const fruit = ['Apple', 'Banana', 'Blueberry', 'Cherry', 'Date']
const label = (s) => s

check(
  M.typeaheadIndex(fruit, label, 'b', -1) === 1,
  'a fresh prefix search starts from the top',
)
check(
  M.typeaheadIndex(fruit, label, 'b', 1) === 2,
  'repeating the same letter cycles to the next match after the current one',
)
check(
  M.typeaheadIndex(fruit, label, 'b', 2) === 1,
  'cycling wraps back around to the first match once the list is exhausted',
)
check(
  M.typeaheadIndex(fruit, label, 'B', -1) === 1,
  'typeahead matching is case-insensitive',
)
check(
  M.typeaheadIndex(fruit, label, 'z', -1) === -1,
  'no match returns -1',
)
check(
  M.typeaheadIndex([], label, 'a', -1) === -1,
  'an empty option list never matches',
)
check(
  M.typeaheadIndex(fruit, label, '', -1) === -1,
  'an empty query never matches',
)

// ── filterBySubstring ─────────────────────────────────────────────────────────

const models = ['meta-llama/Llama-3.1-8B', 'Qwen/Qwen2.5-7B', 'google/gemma-2-9b']

check(
  M.filterBySubstring(models, '').length === models.length,
  'an empty query returns every suggestion, unfiltered',
)
check(
  JSON.stringify(M.filterBySubstring(models, 'llama')) === JSON.stringify([models[0]]),
  'matching is substring, not prefix-only',
)
check(
  JSON.stringify(M.filterBySubstring(models, 'QWEN')) === JSON.stringify([models[1]]),
  'substring matching is case-insensitive',
)
check(
  M.filterBySubstring(models, 'nonexistent').length === 0,
  'no match returns an empty list, not the whole list',
)

// ── capMatches ────────────────────────────────────────────────────────────────

const many = Array.from({ length: 300 }, (_, i) => `model-${i}`)

check(
  M.capMatches(many, 50).shown.length === 50 && M.capMatches(many, 50).hiddenCount === 250,
  'a 300-item list is capped at 50 with the right hidden count',
)
check(
  M.capMatches(['a', 'b'], 50).shown.length === 2 && M.capMatches(['a', 'b'], 50).hiddenCount === 0,
  'a list under the cap is returned whole, with nothing hidden',
)

done()
