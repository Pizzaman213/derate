---
description: Chart pack rules — read before building or changing any chart
alwaysApply: true
---
Charts live in `.omp/charts/` (one API: `.omp/charts/index.ts`; registry: `.omp/charts/registry.json`).
Before building or changing any chart, follow `.omp/charts/RULES.md` (check the registry and
`recommend()` first, build new types inside the pack, append-only premade inputs, icons, run
`cd .omp/charts && npm run charts:check` yourself and only report done when it is green).
