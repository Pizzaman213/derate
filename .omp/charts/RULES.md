# Chart pack rules

The chart pack lives in `.omp/charts/`: one file per chart in `types/<id>/index.ts`, shared
drawing in `types/_shared/`, the engine in `core/`, the API in `index.ts`.
Check: `cd .omp/charts && npm install && npm run charts:typecheck && npm run charts:test`.

1. Check before you create. Look in `types/` first; if an existing type or option covers it, use that. No duplicate chart types.
2. A chart is one file: its input schema, options and drawing in `types/<id>/index.ts`. Add it to the list in `index.ts` and to `LOADERS` in `ui/src/components/PackChart.tsx`. Never build one-off charts directly in app code.
3. The pack holds the chart types the app mounts, not a catalogue. Restore a type from the `charts-full-pack` tag when the app needs it; remove a type, and any `_shared/` code only it used, when the app stops mounting it.
4. After any pack change run the typecheck and tests yourself and only report done when they are green. If the stack changes, update README.md and these rules.
