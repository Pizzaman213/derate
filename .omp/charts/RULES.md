# Chart pack rules

Chart pack lives in `.omp/charts/`; index in `.omp/charts/README.md`, the live list in
`.omp/charts/registry.json`, the API in `.omp/charts/index.ts`, the authoring contract in
`.omp/charts/core/CHART_SPEC.md`. Check: `cd .omp/charts && npm run charts:check`.

These rules apply to every future session in this repo.

1. Check before you create. Before building any chart, search registry.json and use chooser.recommend(). If an existing type or option covers it, use that. No duplicate chart types.
2. New chart types are built inside the pack first (full folder: index, schema, samples with edge cases, tests, README, icon), registered, then used in app code. Never build one-off charts directly in app code.
3. New data shapes or datasets get appended: validate against the schema, add to datasets/ or the chart's samples/, register in datasets.json, and add tests that render with them. Premade inputs are only added to, never silently replaced; a chart type's samples leave with the type (#9).
4. Every new or changed chart gets an icon in icons/, registered in icons.json, matching the icon spec.
5. After any pack change: run charts:check yourself, fix failures, regenerate gallery.html, update registry.json, and add a dated CHANGELOG.md entry (what, why, which files use it).
6. If you find a chart outside the pack (hand-rolled in app code), migrate it into the pack and replace the original.
7. If the stack or tooling changes, update README.md, RULES.md, and the chooser to match. The pack must never drift from the code.
8. You run the checks, not the user. Only report done when everything is green.
9. The pack holds the chart types the app mounts (`ui/src/components/PackChart.tsx`), not a catalogue. Restore a type from the `charts-full-pack` tag (README.md, **Restoring a type**) when the app needs it, and remove a type, with its samples, icon, chooser rule and any `_shared/` module only it used, when the app stops mounting it. Either way, a dated CHANGELOG.md entry.
