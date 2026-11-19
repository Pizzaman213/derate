Design pack lives in `.omp/`; index in `.omp/README.md`. Query the code graph before grepping: `graph_query` tool or `graphify query "<q>" --graph .omp/graph/graph.json`; rebuild with `cd .omp && npm run graph:update`.

These rules apply to every future session in this repo, not just this one.

1. Check before you create. Before adding any icon, color, token, or component, search the pack first (icons.json, tokens.json, components/registry.json). If something premade fits, use it. Don't make duplicates.
2. If nothing fits, add it TO THE PACK FIRST, then use it in code. Never add icons, colors, or components directly in app code.
3. Every new or changed icon goes to .omp/design/icons/svg/, gets registered in icons.json, and you run icons:build + icons:lint.
4. Every new or changed color/token goes into tokens.json at the correct layer, passes tokens:check (contrast), and you run tokens:build.
5. Every new or changed component gets registered in components/registry.json, gets a spec in DESIGN.md, gets tests in .omp/tests/ui/, and passes ui:check.
6. When you find a premade asset outside the pack (an icon someone dropped in, a hardcoded color, an unregistered component), migrate it into the pack and replace the original usage.
7. After any pack change: regenerate index.html, rebuild the Graphify graph, and add a dated entry to CHANGELOG.md (what, why, which files use it).
8. If the stack, folders, or tooling change, update AUDIT.md, DESIGN.md, README.md, and these rules to match. The pack must never drift from the code.
9. Run /pack-sync at the start of any large UI task and before finishing it.
