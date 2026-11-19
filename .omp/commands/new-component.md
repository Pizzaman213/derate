---
description: Scaffold a new shared UI component with gallery states, check, registry row and spec stub
argument-hint: "<Name>"
---

Create the shared component `$ARGUMENTS` for the derate UI, through the design pack.

1. Call `component_lookup` with `$ARGUMENTS` and with each near synonym you can think of. If an existing component already does this, say so, show its usage, and stop — a second component for one job is the failure this pack exists to prevent.
2. Run `cd .omp && npm run ui:new -- $ARGUMENTS`. It writes `ui/src/components/$ARGUMENTS.tsx`, appends gallery states to `.omp/tests/ui/gallery/states.tsx` between `// scaffold:$ARGUMENTS:start/end`, writes `.omp/tests/ui/components/<name>.check.mjs`, adds the registry row, inserts a `### $ARGUMENTS` stub into `.omp/design/DESIGN.md`, rebuilds `design/index.html` and appends a CHANGELOG line.
3. Replace the scaffold's body with the real component. Only `var(--token)` values and `<Icon>`; `token_lookup` for any value you are about to write, `icon_search` for any glyph. Every prop that selects a look is a string-literal union, listed under `variants` in the registry row.
4. Fill the `### $ARGUMENTS` spec in DESIGN.md: anatomy, variants, every state (default, hover, active, focus-visible, disabled, loading, error, empty) or its "n/a — reason", keyboard, ARIA pattern, the component tokens it spends. Add component tokens to `tokens.json` under `component.<name>` if it has parts worth naming.
5. Fill the gallery states to cover every variant × state named in the spec; the registry row's `states` must list them.
6. Run `cd .omp && npm run ui:check -- ui/src/components/$ARGUMENTS.tsx` until green, then complete the `why` line of the CHANGELOG entry.
7. Follow the checklist at the end of `.omp/design/DESIGN.md` ("Adding a component") and the rules in `.omp/RULES.md`, then run `/pack-sync`.
