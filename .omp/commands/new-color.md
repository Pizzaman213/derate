---
description: Add a colour token in the right layer, with its contrast pairs, or answer with the existing one
argument-hint: "<role or value, e.g. 'info' or #3E6194>"
---

Add the colour `$ARGUMENTS` to the derate tokens — or prove it already exists.

1. Call `token_lookup` with `$ARGUMENTS`. By value it returns an exact match or the nearest step with its ΔE; anything within ΔE 3 of an existing step IS that step — use it and **stop**. By role it returns the semantic token that already names the job.
2. Decide the layer by the rule: a ramp step is a **primitive** (`color.<hue>.<step>`, hex, steps ordered by lightness on the 50-grid, intermediates allowed); a role the UI spends is a **semantic** token (`semantic.color.<name>`, references primitives only, one value per theme: `{ "light": …, "dark": …, "high-contrast": … }`); a part of one component is a **component** token (`component.<name>.<part>`, references a semantic token). Colour reports state, never category: a new status hue must sit ≥ 40° from the `tag-*` identity hues (`tabs/chat/tags.check.mjs` asserts it), and nothing is coloured decoratively.
3. Add it to `.omp/design/tokens/tokens.json` with `$type` and a `$description` that says what it is for and, for text, its measured contrast.
4. Add its pairs to `.omp/design/tokens/pairs.json`: text on `panel`/`panel-raised` at 4.5 (7 in high contrast), UI marks at 3. Tune lightness only, never hue, until the gate passes.
5. Run `cd .omp && npm run tokens:build && npm run tokens:check`. Use `contrast_check` while tuning.
6. Use it as `var(--<name>)`; never the hex. The `tool_result` hook rebuilds, appends a CHANGELOG entry and refreshes the graph; complete the entry's `why` line. Follow `.omp/RULES.md`, then run `/pack-sync`.
